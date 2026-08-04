import os
import time
import json
import hashlib
import re
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
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
# UTIL / LOG
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

    TOKEN = vals.get(SSM_TOKEN_PARAM)
    CHAT_ID = vals.get(SSM_CHATID_PARAM)

    # log utile (non stampa il token)
    log("secrets_loaded", token_len=len(TOKEN or ""), chat_id=str(CHAT_ID or "")[:32])


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


def title_has_past_date(title: str) -> bool:
    """
    Se nel titolo c'è una data specifica nel passato (es. 31 gennaio),
    e siamo oltre quella data → ignora.
    """
    import calendar
    from datetime import datetime

    months = {
        "gennaio": 1, "febbraio": 2, "marzo": 3,
        "aprile": 4, "maggio": 5, "giugno": 6,
        "luglio": 7, "agosto": 8, "settembre": 9,
        "ottobre": 10, "novembre": 11, "dicembre": 12
    }

    t = title.lower()
    now = datetime.now()

    for name, num in months.items():
        m = re.search(rf"(\d{{1,2}})\s+{name}", t)
        if m:
            day = int(m.group(1))
            try:
                date_obj = datetime(now.year, num, day)
                if date_obj < now:
                    return True
            except:
                pass

    return False

def telegram_send(text: str):
    """
    Telegram robusto:
    - no parse_mode per evitare 400 da markdown
    - taglio a 3800 char (Telegram max 4096)
    - log completo su HTTP 400/403 ecc (response json)
    """
    get_secrets()

    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TOKEN or CHAT_ID from SSM")

    # guardrail lunghezza
    if len(text) > 3800:
        text = text[:3800].rstrip() + "…"

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    payload_dict = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }

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
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass

        log(
            "telegram_http_error",
            status=getattr(e, "code", None),
            reason=str(e),
            response=(err_body or "")[:1000],
            chat_id=str(CHAT_ID)[:32],
        )
        raise


# ===============================
# MESSAGE FORMAT (UNIFICATO)
# ===============================
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
    text = unescape(text)

    # rimuovi "Leggi tutto..." e simili
    for marker in ["Leggi tutto...", "Leggi tutto", "Continua...", "Continua"]:
        text = text.replace(marker, "")

    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines).strip()


def clean_text(text: str) -> str:
    text = re.sub(r"Leggi tutto\.{0,3}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+\n", "\n", text)
    return text.strip()


def normalize_body(source: str, title: str, body: str, max_len: int = 900) -> str:
    """
    Normalizzazione unica per TUTTI i flussi.
    Stile compatto (come piace a te), niente elenchi imposti:
    - pulizia HTML se serve
    - compattazione whitespace
    - rimozione "Leggi tutto"
    - se troppo lungo: prime 1-2 frasi + eventuale frase di chiusura
    """
    s = (body or "").strip()
    if not s:
        return ""

    # Se sembra HTML, pulisci
    if "<" in s and ">" in s:
        s = strip_html(s)

    s = clean_text(s)
    s = unescape(s)

    # Collassa spazi e righe in modo umano
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    s = re.sub(r"\b(leggi tutto|continua)\b\.{0,3}", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\bleggi avviso\b\.?", "", s, flags=re.IGNORECASE)
    # rimuovi marker finali
    s = re.sub(r"\b(leggi tutto|continua)\b\.{0,3}\s*$", "", s, flags=re.IGNORECASE).strip()

    # Se evento "urgente" (sospensioni/guasti) -> taglio semplice, no riassunto
    t = (title or "").lower()
    urgent = any(k in (t + " " + s).lower() for k in [
        "temporaneamente sospes", "servizio sospes", "circolazione sospes",
        "interru", "guasto", "soccorso", "incidente"
    ])
    if urgent:
        return s[:max_len].rstrip() + ("…" if len(s) > max_len else "")

    # Se lungo: prendi incipit (1-2 frasi) + micro-chiusura per GTT/RFI
    if len(s) > max_len:
        one_line = re.sub(r"\s+", " ", s).strip()
        parts = re.split(r"(?<=[.!?])\s+", one_line)
        incipit = " ".join(parts[:2]).strip()

        extra = ""
        low = one_line.lower()
        if source in ("TRASPORTO PUBBLICO (GTT)", "FERROVIE (RFI)"):
            if any(x in low for x in ["servizio sostitutivo", "bus sostitutivo"]):
                extra = "Possibile attivazione servizio sostitutivo."
            elif "calendario" in low or re.search(r"\btra il \d{1,2}\b", low):
                extra = "Dettagli e calendario nel link."
            elif "lavori" in low:
                extra = "Dettagli nel link."

        out = incipit
        if extra and extra.lower() not in out.lower():
            out = (out + " " + extra).strip()

        out = out[:max_len].rstrip() + "…"
        return out

    # Default: se il testo ha mille “a capo” spezzati, comprimilo in paragrafo
    if "\n" in s:
        s = re.sub(r"\s*\n\s*", "\n", s)            # trim per riga
        s = re.sub(r"\n{3,}", "\n\n", s).strip()    # max doppio a capo
    return s


def render_event(ev: dict) -> str:
    """
    Formatter unico: stesso layout per tutti.
    """
    source = (ev.get("source") or "INFO").strip()
    sev = (ev.get("severity") or "INFO").strip()
    title = (ev.get("title") or "").strip()
    body = (ev.get("body") or "").strip()
    link = (ev.get("link") or "").strip()

    body = normalize_body(source, title, body, max_len=int(ev.get("max_len", 900)))

    msg = format_msg(source, sev, title, body=body, link=link)

    # guardrail telegram
    if len(msg) > 3800:
        msg = msg[:3800].rstrip() + "…"

    return msg


# ===============================
# LINE EXTRACTION (KEEP FUNCTIONS)
# ===============================
def extract_line(title: str):
    """
    Versione più permissiva (la tenevi già).
    ATTENZIONE: può prendere numeri da date.
    """
    t = title.upper()
    if "METRO" in t:
        return "METROPOLITANA"
    m = re.search(r"\b\d{1,3}[A-Z]{0,2}\b", t)
    if m:
        return f"LINEA {m.group(0)}"
    return None


def extract_line_strict(title: str):
    """
    Versione robusta: prende SOLO 'LINEA <codice>' oppure METRO.
    """
    t = title.upper()
    if "METROPOLITANA" in t or "METRO" in t:
        return "METROPOLITANA"
    m = re.search(r"\bLINEA\s+([0-9]{1,3}[A-Z]{0,3})\b", t)
    if m:
        return f"LINEA {m.group(1)}"
    return None


# ===============================
# GTT HELPERS (KEEP FUNCTIONS)
# ===============================
def gtt_smart_body(title: str, summary: str, max_len: int = 800) -> str:
    """
    Rimane per compatibilità (non obbligatoria),
    ma ora la normalizzazione "vera" la fa normalize_body().
    """
    t = (title or "").lower()
    s = clean_text(summary or "")
    s = re.sub(r"\s+", " ", s).strip()

    if not s:
        return ""

    if any(k in t for k in ["temporaneamente sospeso", "sospesa", "riprende regolare", "riprende", "interrotta"]):
        return s[:max_len]

    is_metro = ("metropolitana" in t) or ("metro" in t)
    if is_metro and len(s) > max_len:
        parts = re.split(r"(?<=[.!?])\s+", s)
        incipit = " ".join(parts[:2]).strip()

        extra = ""
        for key in ["servizio sostitutivo", "bus", "stazioni", "tratta", "fermi", "bengasi", "porta nuova", "nizza", "dante", "bernini"]:
            if key in s.lower():
                extra = "Possibili disservizi: previsto servizio sostitutivo bus se necessario."
                break

        out = incipit
        if extra and extra.lower() not in out.lower():
            out = out + " " + extra

        return out[:max_len].rstrip()

    if len(s) > max_len:
        return (s[:max_len].rstrip() + "…")

    return s


def gtt_is_torino_city(link: str, text_lower: str) -> bool:
    lk = (link or "").lower()
    if "/torino-e-cintura/" in lk:
        return True
    if "metropolitana" in text_lower or "metro" in text_lower:
        return True
    return False


# ===============================
# COLLECTORS
# ===============================
def collect_arpa():
    try:
        content = http_get_bytes(ARPA_URL)
        root = ET.fromstring(content)
        blob = ET.tostring(root, encoding="unicode")

        if "TORINO" not in blob.upper():
            log("source_collected", source="ARPA", events=0)
            return []

        eid = "arpa:" + sha(blob)

        event = {
            "source": "ALLERTA METEO",
            "severity": "LOW",
            "title": "Aggiornamento ARPA Piemonte",
            "body": "",
            "link": "https://www.arpa.piemonte.it",
            "max_len": 900
        }
        msg = render_event(event)

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

            # scarta pagine filtro (argomenti / liste)
            if "?f%5B0%5D=" in href or "?f[0]=" in href or "?" in href:
                continue

            if not href.startswith("/novita/"):
                continue

            if not any(k in title.lower() for k in ["viabilità", "chius", "deviaz", "lavori"]):
                continue

            full = "https://www.comune.torino.it" + href
            eid = "comune:" + sha(title + full)

            event = {
                "source": "VIABILITÀ / CANTIERI",
                "severity": "MED",
                "title": title,
                "body": "",
                "link": full,
                "max_len": 900
            }
            msg = render_event(event)
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
            if "?f%5B0%5D=" in href or "?f[0]=" in href or "?" in href:
                continue

            if not href.startswith("/novita/"):
                continue

            if not any(k in title.lower() for k in ["limitazioni", "livello", "smog"]):
                continue

            full = "https://www.comune.torino.it" + href
            eid = "smog:" + sha(title + full)

            event = {
                "source": "LIMITAZIONI / SMOG",
                "severity": "LOW",
                "title": title,
                "body": "",
                "link": full,
                "max_len": 900
            }
            if title_has_past_date(title):
                continue
            msg = render_event(event)
            events.append((eid, msg))

        log("source_collected", source="SMOG", events=len(events))
        return events

    except Exception as e:
        log("source_error", source="SMOG", error=str(e))
        return []


def parse_gtt_rss(feed):
    events = []

    GTT_EVENT_KEYWORDS = [
        "linea", "linee",
        "metro", "metropolitana",
        "sospes", "interru",
        "guasto",
        "devia", "deviat",
        "limit",
        "ripristin",
        "impedimento",
        "parcheggio",
        "servizio sostitutivo"
    ]

    for entry in feed.entries:
        title = (entry.title or "").strip()
        summary_html = (getattr(entry, "summary", "") or "").strip()
        link = (entry.link or "").strip()

        summary = strip_html(summary_html)
        summary = gtt_smart_body(title, summary, max_len=900)

        text = (title + " " + summary).lower()
        if any(x in text for x in [
            "iniziativa",
            "progetto",
            "settimana della musica",
            "personalizzati",
            "community",
            "engagement",
            "grafica dedicata",
            "wrappati"
        ]):
            continue
        
        is_parking = "parcheggio" in text

        # Solo Torino città/cintura
        if not gtt_is_torino_city(link, text):
            continue

        # Deve parlare di linee o metro
        if not any(k in text for k in ["linea", "linee", "metro","parcheggio"]):
            continue

        # Evento operativo
        if not any(w in text for w in GTT_EVENT_KEYWORDS):
            continue

        # Severità
        if any(x in text for x in ["circolazione sospes", "sospes", "interru", "guasto"]):
            sev = "HIGH"
        elif "ripristin" in text:
            sev = "INFO"
        else:
            sev = "MED"

        line = extract_line_strict(title)
        if line and title.upper().startswith("LINEA"):
            title = title  # non duplicare
        elif line:
            title = f"{line} — {title}"

        eid = "gttrss:" + sha(title + link + summary)
        if is_parking and not title.startswith("🅿️"):
            title = f"🅿️ {title}"

        event = {
            "source": "TRASPORTO PUBBLICO (GTT)",
            "severity": sev,
            "title": title,
            "body": summary,
            "link": link or "https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio",
            "max_len": 900
        }

        msg = render_event(event)
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
        
        if any(x in text for x in [
            "iniziativa",
            "progetto",
            "settimana della musica",
            "personalizzati",
            "community",
            "engagement",
            "grafica dedicata",
            "wrappati"
        ]):
            continue
        is_parking = "parcheggio" in text
        # deve parlare di linee
        if not any(k in text for k in ["linea", "linee", "metro","parcheggio"]):
            continue

        # evento operativo
        if not any(w in text for w in [
            "sospes", "interru", "guasto",
            "devia", "deviat",
            "limit",
            "ripristin",
            "impedimento"
        ]):
            continue

        if any(x in text for x in ["sospes", "interru", "guasto"]):
            sev = "HIGH"
        elif "ripristin" in text:
            sev = "INFO"
        else:
            sev = "MED"

        line = extract_line_strict(title) or extract_line(title)
        if line and title.upper().startswith("LINEA"):
            title = title  # non duplicare
        elif line:
            title = f"{line} — {title}"

        eid = "gttraw:" + sha(title + body)

        if is_parking and not title.startswith("🅿️"):
            title = f"🅿️ {title}"
        event = {
            "source": "TRASPORTO PUBBLICO (GTT)",
            "severity": sev,
            "title": title,
            "body": body,
            "link": "https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio",
            "max_len": 900
        }

        msg = render_event(event)
        events.append((eid, msg))

    log("source_collected", source="GTT_RAW", events=len(events))
    return events


def collect_gtt():
    events = []

    # Prima RAW (più live)
    raw_events = collect_gtt_raw()
    if raw_events:
        events += raw_events

    # Poi RSS come backup
    feed = feedparser.parse(GTT_RSS)
    if getattr(feed, "entries", None):
        events += parse_gtt_rss(feed)

    return events


def _classify_rfi(text_lower: str) -> str:
    if any(x in text_lower for x in ["circolazione sospesa", "interruzion", "sospes"]):
        return "HIGH"
    if any(x in text_lower for x in ["guasto", "avaria", "problema tecnico"]):
        return "HIGH"
    if any(x in text_lower for x in ["rallent", "ritard"]):
        return "MED"
    if "lavori" in text_lower or "manutenzione" in text_lower:
        return "MED"
    if any(x in text_lower for x in ["ripristinat", "regolare"]):
        return "INFO"
    return "MED"


def collect_rfi():
    try:
        feed = feedparser.parse(RFI_URL)
        events = []

        # parole chiave evento che CI INTERESSANO
        RFI_EVENT_KEYWORDS = [
            "lavori",
            "manutenzione",
            "interruzion",
            "circolazione sospes",
            "circolazione rallent",
            "rallent",
            "ritard",
            "guasto",
            "problema tecnico",
            "avaria",
            "sciopero"
        ]

        # Torino area (non tutta Italia)
        RFI_TORINO_KEYWORDS = [
            "torino",
            "porta nuova",
            "porta susa",
            "lingotto",
            "stura",
            "rebaudengo",
            "susa",
            "bardonecchia",
            "chivasso",
            "ivrea",
            "pinerolo",
            "novara",
            "asti",
            "alessandria"
        ]

        for entry in feed.entries:
            title = (entry.title or "").strip()
            summary = (entry.summary.strip() if hasattr(entry, "summary") else "")
            link = (entry.link or "").strip()

            text = (title + " " + summary).lower()

            # 1️⃣ Deve riguardare area Torino
            if not any(k in text for k in RFI_TORINO_KEYWORDS):
                continue

            # 2️⃣ Deve essere evento operativo (non informativo)
            if not any(k in text for k in RFI_EVENT_KEYWORDS):
                continue

            # 3️⃣ Escludi mega elenchi nazionali (troppi separatori)
            if title.count(",") >= 4:
                continue

            # Severità intelligente
            if any(k in text for k in ["circolazione sospes", "interruzion"]):
                sev = "HIGH"
            elif any(k in text for k in ["guasto", "avaria", "problema tecnico"]):
                sev = "HIGH"
            elif any(k in text for k in ["rallent", "ritard"]):
                sev = "MED"
            elif "lavori" in text or "manutenzione" in text:
                sev = "MED"
            else:
                sev = "MED"

            eid = "rfi:" + sha(title + link + summary)

            event = {
                "source": "FERROVIE (RFI)",
                "severity": sev,
                "title": title,
                "body": summary,
                "link": link,
                "max_len": 900
            }

            msg = render_event(event)
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
        events += (collect_arpa() or [])
        events += (collect_gtt() or [])
        events += (collect_rfi() or [])
        events += (collect_comune_comunicati() or [])
        events += (collect_comune_smog() or [])

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
                continue

            try:
                ddb_mark(eid)
                log("ddb_mark_ok", event_id=eid)
            except Exception as e:
                log("ddb_mark_failed", event_id=eid, error=str(e))

            sent += 1

        duration = round(time.time() - start, 2)
        log("run_summary", sent=sent, total=total, duration=duration)

        return {"status": "ok", "sent": sent, "total": total, "duration": duration}

    except Exception as e:
        log("fatal_error", error=str(e))
        raise