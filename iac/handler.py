import os
import time
import json
import hashlib
import re
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import urllib.request
from html import unescape

import feedparser
import boto3
from botocore.config import Config
from bs4 import BeautifulSoup


# ===============================
# AWS CLIENTS
# ===============================
_cfg = Config(retries={"max_attempts": 3, "mode": "standard"})
ssm = boto3.client("ssm", config=_cfg)
ddb = boto3.client("dynamodb", config=_cfg)


# ===============================
# ENV
# ===============================
ARPA_URL = "https://www.arpa.piemonte.it/export/xmlcap/allerta.xml"
GTT_RSS = "https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio?format=feed&type=rss"
GTT_RAW_URL = "https://www.gtt.to.it/cms/index.php?option=com_gtt&priorita=1&tmpl=raw&view=avvisi"
COMUNE_COMUNICATI_URL = "https://www.comune.torino.it/novita/comunicati"
COMUNE_AVVISI_URL = "https://www.comune.torino.it/novita/avvisi"
RFI_URL = "https://www.rfi.it/content/rfi/it/news-e-media/infomobilita.rss.updates.piemonte.xml"

DDB_TABLE = os.environ["DDB_TABLE"]
TTL_SECONDS = int(os.environ.get("TTL_SECONDS", "172800"))

SSM_TOKEN_PARAM = os.environ["SSM_TOKEN_PARAM"]
SSM_CHATID_PARAM = os.environ["SSM_CHATID_PARAM"]


# ===============================
# RUNTIME CACHE
# ===============================
TOKEN = None
CHAT_ID = None

gtt_failures = 0
gtt_skip_until = 0


# ===============================
# UTIL
# ===============================
def log(event_type, **kwargs):
    print(json.dumps({
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        **kwargs
    }))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def http_get_text(url: str, timeout=20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "TorinoAlertBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def http_get_bytes(url: str, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "TorinoAlertBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_secrets():
    global TOKEN, CHAT_ID
    if TOKEN and CHAT_ID:
        return
    resp = ssm.get_parameters(
        Names=[SSM_TOKEN_PARAM, SSM_CHATID_PARAM],
        WithDecryption=True
    )
    vals = {p["Name"]: p["Value"] for p in resp["Parameters"]}
    TOKEN = vals[SSM_TOKEN_PARAM]
    CHAT_ID = vals[SSM_CHATID_PARAM]


def ddb_seen(event_id: str) -> bool:
    r = ddb.get_item(
        TableName=DDB_TABLE,
        Key={"event_id": {"S": event_id}},
        ConsistentRead=False
    )
    return "Item" in r


def ddb_mark(event_id: str):
    expires_at = int(time.time()) + TTL_SECONDS
    ddb.put_item(
        TableName=DDB_TABLE,
        Item={
            "event_id": {"S": event_id},
            "expires_at": {"N": str(expires_at)}
        },
        ConditionExpression="attribute_not_exists(event_id)"
    )


#def telegram_send(text: str):
#    get_secrets()
#    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
#
#    payload = json.dumps({
#        "chat_id": CHAT_ID,
#        "text": text,
#        "parse_mode": "Markdown",
#        "disable_web_page_preview": True
#    }).encode("utf-8")
#
#    req = urllib.request.Request(
#        url,
#        data=payload,
#        headers={"Content-Type": "application/json"},
#        method="POST"
#    )
#
#    with urllib.request.urlopen(req, timeout=15) as resp:
#        resp.read()

def telegram_send(text: str):
    get_secrets()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    payload_dict = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }

    # evita errori markdown: per ora togliamo parse_mode
    # (quando vuoi lo rimettiamo con escaping serio)
    # payload_dict["parse_mode"] = "Markdown"

    payload = json.dumps(payload_dict).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return body
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else ""
        log("telegram_http_error",
            status=getattr(e, "code", None),
            reason=str(e),
            response=err_body[:1000],
            chat_id=str(CHAT_ID)[:32]
        )
        raise

# ===============================
# COLLECTORS
# ===============================
def collect_arpa():
    try:
        content = http_get_bytes(ARPA_URL)
        root = ET.fromstring(content)
        blob = ET.tostring(root, encoding="unicode")

        if "TORINO" not in blob.upper():
            return []

        eid = "arpa:" + sha(blob)

        msg = "🌧 ALLERTA METEO — TORINO\n\nAggiornamento ARPA Piemonte\n\n👉 https://www.arpa.piemonte.it"

        log("source_collected", source="ARPA", events=1)
        return [(eid, msg)]

    except Exception as e:
        log("source_error", source="ARPA", error=str(e))
        return []

def collect_comune_comunicati():
    try:
        html = http_get_text(COMUNE_COMUNICATI_URL)
        soup = BeautifulSoup(html, "html.parser")

        events = []

        for a in soup.select("a"):
            title = a.get_text(" ", strip=True)
            href = a.get("href") or ""

            if not title or len(title) < 10:
                continue
            
            # 🔥 SCARTA PAGINE FILTRO (argomenti)
            if "?f%5B0%5D=" in href or "?f[0]=" in href:
                continue
            
            if not href.startswith("/novita/"):
                continue
            
            if not any(k in title.lower() for k in ["viabilità", "chius", "deviaz", "lavori"]):
                continue
            
            full = "https://www.comune.torino.it" + href
            eid = "comune:" + sha(title + full)

            msg = f"🚧 COMUNE — TORINO\n\n{title}\n\n👉 {full}"
            events.append((eid, msg))

        log("source_collected", source="COMUNE", events=len(events))
        return events

    except Exception as e:
        log("source_error", source="COMUNE", error=str(e))
        return []

def collect_comune_smog():
    try:
        html = http_get_text(COMUNE_AVVISI_URL)
        soup = BeautifulSoup(html, "html.parser")

        events = []

        for a in soup.select("a"):
            title = a.get_text(" ", strip=True)
            href = a.get("href") or ""

            if not title or len(title) < 10:
                continue

            # scarta pagine filtro/lista
            if "?" in href:
                continue

            if not href.startswith("/novita/"):
                continue

            if not any(k in title.lower() for k in ["limitazioni", "livello", "smog"]):
                continue

            full = "https://www.comune.torino.it" + href
            eid = "smog:" + sha(title + full)

            msg = f"🌫 LIMITAZIONI / SMOG — TORINO\n\n{title}\n\n👉 {full}"
            events.append((eid, msg))

        log("source_collected", source="SMOG", events=len(events))
        return events

    except Exception as e:
        log("source_error", source="SMOG", error=str(e))
        return []

#def collect_gtt():
#    global gtt_failures, gtt_skip_until
#
#    if time.time() < gtt_skip_until:
#        log("gtt_skip_active")
#        return []
#
#    try:
#        html = http_get_text(GTT_RAW_URL)
#        gtt_failures = 0
#    except Exception as e:
#        gtt_failures += 1
#        log("gtt_error", failures=gtt_failures, error=str(e))
#
#        if gtt_failures >= 3:
#            gtt_skip_until = time.time() + 600
#            log("gtt_circuit_breaker_activated")
#
#        return []
#
#    soup = BeautifulSoup(html, "html.parser")
#    events = []
#
#    for h4 in soup.find_all(["h4", "h3"]):
#        title = h4.get_text(" ", strip=True)
#        if not title:
#            continue
#
#        body = ""
#        for sib in h4.next_siblings:
#            if getattr(sib, "name", None) in ["h4", "h3"]:
#                break
#            if getattr(sib, "get_text", None):
#                body += sib.get_text(" ", strip=True) + " "
#
#        text = (title + body).lower()
#
#        if not any(w in text for w in ["linea", "metro", "sospes", "interru", "guasto"]):
#            continue
#
#        eid = "gtt:" + sha(title + body)
#        msg = f"🚇 GTT — TORINO\n\n{title}\n\n{body.strip()}"
#
#        events.append((eid, msg))
#
#    log("source_collected", source="GTT", events=len(events))
#    return events


def extract_line(title: str):
    """
    Estrae numero linea o 'Metropolitana'
    """
    t = title.upper()

    # Metropolitana
    if "METRO" in t:
        return "METROPOLITANA"

    # Linea 4, 10, 16CD ecc.
    m = re.search(r"\b\d{1,3}[A-Z]{0,2}\b", t)
    if m:
        return f"LINEA {m.group(0)}"

    return None

def format_msg(source: str, severity: str, title: str, body: str = "", link: str = ""):

    emoji_map = {
        "CRIT": "🔴",
        "HIGH": "🟠",
        "MED": "🟡",
        "LOW": "🟢",
        "INFO": "ℹ️"
    }

    emoji = emoji_map.get(severity, "ℹ️")

    msg = f"{emoji} {source} — TORINO\n\n{title}"

    if body:
        msg += f"\n\n{body}"

    if link:
        msg += f"\n\n👉 {link}"

    return msg

def strip_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text("\n", strip=True)

    # normalizza
    text = unescape(text)

    # 🔥 rimuovi "Leggi tutto..." e simili
    for marker in ["Leggi tutto...", "Leggi tutto", "Continua...", "Continua"]:
        text = text.replace(marker, "")

    # pulizia righe vuote
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    return "\n".join(lines).strip()

def clean_text(text: str) -> str:
    text = re.sub(r"Leggi tutto\.{0,3}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+\n", "\n", text)
    return text.strip()

def gtt_smart_body(title: str, summary: str, max_len: int = 800) -> str:
    """
    Restituisce un body compatto, stile 'paragrafo', senza elenchi.
    - Per avvisi brevi: lascia quasi tutto
    - Per avvisi lunghi: incipit + sintesi (1 riga), poi taglio
    """
    t = (title or "").lower()
    s = clean_text(summary or "")
    s = re.sub(r"\s+", " ", s).strip()  # una sola riga "umana"

    if not s:
        return ""

    # Se è un "servizio sospeso / ripristinato" deve rimanere secco e diretto
    if any(k in t for k in ["temporaneamente sospeso", "sospesa", "riprende regolare", "riprende", "interrotta"]):
        return s[:max_len]

    # Metro / lavori lunghi: prendiamo incipit + mini-sintesi
    is_metro = ("metropolitana" in t) or ("metro" in t)

    if is_metro and len(s) > max_len:
        # incipit: prime 2 frasi circa (split semplice)
        parts = re.split(r"(?<=[.!?])\s+", s)
        incipit = " ".join(parts[:2]).strip()

        # sintesi: cerchiamo una frase utile se presente
        extra = ""
        for key in ["servizio sostitutivo", "bus", "stazioni", "tratta", "fermi", "bengasi", "porta nuova", "nizza", "dante", "bernini"]:
            if key in s.lower():
                extra = "Possibili disservizi: previsto servizio sostitutivo bus se necessario."
                break

        out = incipit
        if extra and extra.lower() not in out.lower():
            out = out + " " + extra

        return out[:max_len].rstrip()

    # Default: se è lungo, taglia e basta (senza cambiare stile)
    if len(s) > max_len:
        return (s[:max_len].rstrip() + "…")

    return s

def extract_line_strict(title: str):
    # prende SOLO se c'è "LINEA <qualcosa>" nel titolo, evitando date tipo "16 febbraio"
    m = re.search(r"\bLINEA\s+([0-9]{1,3}[A-Z]{0,3})\b", title.upper())
    if m:
        return f"LINEA {m.group(1)}"
    if "METROPOLITANA" in title.upper() or "METRO" in title.upper():
        return "METROPOLITANA"
    return None

def gtt_is_torino_city(link: str, text_lower: str) -> bool:
    # regola forte: tieni TORINO E CINTURA e metro.
    # scarta "provincia-piemonte" (Ciriè, Chivasso, Giaveno ecc.)
    lk = (link or "").lower()
    if "/torino-e-cintura/" in lk:
        return True
    if "metropolitana" in text_lower or "metro" in text_lower:
        return True
    return False

def parse_gtt_rss(feed):
    events = []

    for entry in feed.entries:
        title = (entry.title or "").strip()
        summary_html = (getattr(entry, "summary", "") or "").strip()
        link = (entry.link or "").strip()

        summary = strip_html(summary_html)
        summary = gtt_smart_body(title, summary, max_len=900)
        text = (title + " " + summary).lower()

        # Solo Torino città/cintura
        if not gtt_is_torino_city(link, text):
            continue

        # filtro "utile"
        if not any(w in text for w in ["linea", "metro", "metropolitana", "sospes", "interru", "guasto", "devia", "scioper"]):
            continue

        # severità
        sev = "MED"
        if any(x in text for x in ["circolazione sospesa", "sospes", "interru", "guasto"]):
            sev = "HIGH"
        elif any(x in text for x in ["ripristin", "regolare servizio"]):
            sev = "INFO"

        # evidenzia linea (strict)
        line = extract_line_strict(title)
        if line:
            title = f"{line} — {title}"

        # dedup
        eid = "gttrss:" + sha(title + link + summary)

        msg = format_msg(
            "TRASPORTO PUBBLICO (GTT)",
            sev,
            title,
            body=summary,
            link=link or "https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio"
        )

        events.append((eid, msg))

    log("source_collected", source="GTT_RSS", events=len(events))
    return events

def collect_gtt_raw():
    global gtt_failures, gtt_skip_until

    if time.time() < gtt_skip_until:
        log("gtt_skip_active")
        return []

    try:
        html = http_get_text(GTT_RAW_URL)
        gtt_failures = 0
    except Exception as e:
        gtt_failures += 1
        log("gtt_error", failures=gtt_failures, error=str(e))

        if gtt_failures >= 3:
            gtt_skip_until = time.time() + 600
            log("gtt_circuit_breaker_activated", seconds=600)

        return []

    soup = BeautifulSoup(html, "html.parser")
    events = []

    for h4 in soup.find_all(["h4", "h3"]):
        title = h4.get_text(" ", strip=True)
        if not title or "Avvisi ultima ora" in title:
            continue

        body_parts = []
        for sib in h4.next_siblings:
            if getattr(sib, "name", None) in ["h4", "h3"]:
                break
            if getattr(sib, "get_text", None):
                t = sib.get_text(" ", strip=True)
                if t:
                    body_parts.append(t)

        body = " ".join(body_parts).strip()
        text = (title + " " + body).lower()

        if not any(w in text for w in ["linea", "metro", "metropolitana", "sospes", "interru", "guasto", "devia", "scioper", "parcheggio"]):
            continue

        sev = "MED"
        if any(x in text for x in ["sospes", "interru", "guasto"]):
            sev = "HIGH"
        elif "ripristin" in text or "regolare" in text:
            sev = "INFO"
        elif "parcheggio" in text:
            sev = "INFO"

        line = extract_line(title)
        if line:
            title = f"{line} — {title}"

        eid = "gttraw:" + sha(title + body)

        msg = format_msg(
            "TRASPORTO PUBBLICO (GTT)",
            sev,
            title,
            body=body,
            link="https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio"
        )

        events.append((eid, msg))

    log("source_collected", source="GTT_RAW", events=len(events))
    return events

def collect_gtt():
    try:
        feed = feedparser.parse(GTT_RSS)
        if feed.entries:
            log("gtt_rss_ok", count=len(feed.entries))
            return parse_gtt_rss(feed)
        else:
            log("gtt_rss_empty_fallback")
            return collect_gtt_raw()
    except Exception as e:
        log("gtt_rss_error", error=str(e))
        return collect_gtt_raw()

def collect_rfi():
    try:
        feed = feedparser.parse(RFI_URL)
        events = []

        for entry in feed.entries:
            title = entry.title.strip()
            summary = entry.summary.strip() if hasattr(entry, "summary") else ""
            text = (title + " " + summary).lower()

            if "torino" not in text:
                continue

            eid = "rfi:" + sha(title + entry.link)

            msg = f"🚆 FERROVIE (RFI) — TORINO\n\n{title}\n\n{summary}\n\n👉 {entry.link}"
            events.append((eid, msg))

        log("source_collected", source="RFI", events=len(events))
        return events

    except Exception as e:
        log("source_error", source="RFI", error=str(e))
        return []


# ===============================
# MAIN HANDLER
# ===============================
def lambda_handler(event, context):

    start = time.time()
    log("run_started")

    try:
        events = []
        events += collect_arpa()
        events += collect_gtt()
        events += collect_rfi()
        events += collect_comune_comunicati()
        events += collect_comune_smog()

        total = len(events)
        sent = 0

        for eid, msg in events:
            if ddb_seen(eid):
                log("dedup_skipped", event_id=eid)
                continue
            
            try:
                log("sending", event_id=eid)
                telegram_send(msg)
                log("sent_ok", event_id=eid)
            except Exception as e:
                log("send_failed", event_id=eid, error=str(e))
                # NON raise: vai avanti con gli altri messaggi
                continue
            
            try:
                ddb_mark(eid)
                log("ddb_mark_ok", event_id=eid)
            except Exception as e:
                log("ddb_mark_failed", event_id=eid, error=str(e))

            sent += 1

        duration = round(time.time() - start, 2)

        log("run_summary", sent=sent, total=total, duration=duration)

        return {
            "status": "ok",
            "sent": sent,
            "total": total,
            "duration": duration
        }

    except Exception as e:
        log("fatal_error", error=str(e))
        raise