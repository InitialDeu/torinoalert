import os
import time
import requests
import json
import hashlib
import re
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import time
from bs4 import BeautifulSoup



BOT_TOKEN = os.environ["TORINOALERT_BOT_TOKEN"]
CHAT_ID = os.environ["TORINOALERT_CHAT_ID"]


POLL_SECONDS = 120
STATE_FILE = "seen.json"

# Fonti
ARPA_URL = "https://www.arpa.piemonte.it/export/xmlcap/allerta.xml"
GTT_RAW_URL = "https://www.gtt.to.it/cms/index.php?option=com_gtt&priorita=1&tmpl=raw&view=avvisi"
COMUNE_COMUNICATI_URL = "https://www.comune.torino.it/novita/comunicati"
COMUNE_AVVISI_URL = "https://www.comune.torino.it/novita/avvisi"

# Filtri keyword
KW_VIABILITA = [
    "viabil", "traff", "circol", "chius", "deviaz", "cantiere", "lavori",
    "senso unico", "limitazioni", "divieto", "modifiche alla viabilità", "strada", "corso", "piazza"
]
KW_SMOG = [
    "smog", "semaforo", "limitazioni alla circolazione", "circolazione veicolare", "pm10", "livello"
]

# Severità GTT (semplice ma efficace)
GTT_CRIT = ["metropolitana", "metro", "sospes", "interrott", "chius", "guasto", "sciopero"]
GTT_WARN = ["deviaz", "ritard", "lavori", "modific"]
gtt_failures = 0
gtt_skip_until = 0
# ========== UTIL ==========
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # data: {id: timestamp_iso}
            if isinstance(data, dict):
                return data
    except:
        pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

STATE = load_state()

def seen_before(event_id: str) -> bool:
    return event_id in STATE

def mark_seen(event_id: str):
    STATE[event_id] = now_iso()

def cleanup_state(max_items=4000):
    # semplice: se cresce troppo, tieni solo gli ultimi N (per timestamp)
    if len(STATE) <= max_items:
        return
    items = sorted(STATE.items(), key=lambda kv: kv[1], reverse=True)[:max_items]
    STATE.clear()
    STATE.update(dict(items))

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }, timeout=15)
    # utile in debug locale:
    if r.status_code >= 300:
        print("Telegram error:", r.status_code, r.text)

# ========== EVENT MODEL ==========
def sev_emoji(sev: str) -> str:
    return {"CRIT": "🔴", "HIGH": "🟠", "MED": "🟡", "LOW": "🟢", "INFO": "ℹ️"}.get(sev, "ℹ️")

def format_msg(category: str, sev: str, title: str, body: str = "", link: str = "") -> str:
    e = sev_emoji(sev)
    out = [f"{e} *{category} — TORINO*", "", f"*{title}*"]
    if body:
        out += ["", body.strip()]
    if link:
        out += ["", f"👉 {link}"]
    return "\n".join(out).strip()

# ========== SOURCES ==========
def fetch_text(url: str) -> str:
    r = requests.get(url, timeout=20, headers={"User-Agent": "TorinoAlertBot/1.0"})
    r.raise_for_status()
    return r.text

def fetch_bytes(url: str) -> bytes:
    r = requests.get(url, timeout=20, headers={"User-Agent": "TorinoAlertBot/1.0"})
    r.raise_for_status()
    return r.content

# --- ARPA (XML CAP) ---
def arpa_severity(xml_blob_upper: str) -> str:
    # euristica: spesso nei testi compaiono "ROSSO/ARANCIONE/GIALLO/VERDE"
    if "ROSS" in xml_blob_upper:
        return "CRIT"
    if "ARANC" in xml_blob_upper:
        return "HIGH"
    if "GIALL" in xml_blob_upper:
        return "MED"
    if "VERD" in xml_blob_upper:
        return "LOW"
    return "INFO"

def collect_arpa():
    content = fetch_bytes(ARPA_URL)
    root = ET.fromstring(content)
    blob = ET.tostring(root, encoding="unicode")
    up = blob.upper()

    # “Solo Torino città”: il feed è regionale → filtro testo “TORINO”
    if "TORINO" not in up:
        return []

    sev = arpa_severity(up)
    # dedup: hash dell’intero documento (quando cambia, notifichi)
    eid = "arpa:" + sha(blob)

    if seen_before(eid):
        return []

    title = "Aggiornamento allerta meteo ARPA Piemonte"
    msg = format_msg("ALLERTA METEO", sev, title, link="https://www.arpa.piemonte.it")
    return [(eid, msg)]

# --- GTT (raw) ---
def gtt_severity(text_lower: str) -> str:
    if any(k in text_lower for k in GTT_CRIT):
        return "HIGH"  # spesso critico per utenti, ma non sempre emergenza “rossa”
    if any(k in text_lower for k in GTT_WARN):
        return "MED"
    return "INFO"

def extract_line(title):
    m = re.search(r"(linea\s+\S+|metropolitana|metro)", title.lower())
    if m:
        return m.group(0).upper()
    return None

GTT_WHITELIST = [
    "metro", "metropolitana", "sospes", "interr", "guasto",
    "servizio sostitutivo", "deviaz", "scioper", "linea","parcheggio" , "manifestazione"
]

GTT_BLACKLIST = [
    "evento", "servizio speciale","basket"
]

def collect_gtt():

    global gtt_failures, gtt_skip_until

    import time

    # circuito breaker — skip se attivo
    if time.time() < gtt_skip_until:
        print("GTT skip attivo")
        return []

    try:
        html = fetch_text(GTT_RAW_URL)
        gtt_failures = 0

    except Exception as e:
        gtt_failures += 1
        print("GTT errore:", e)

        if gtt_failures >= 3:
            print("GTT circuit breaker attivo per 10 min")
            gtt_skip_until = time.time() + 600

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
                body_parts.append(sib.get_text(" ", strip=True))

        body = " ".join(body_parts).strip()
        text = (title + " " + body).lower()

        # filtro contenuti
        if any(b in text for b in GTT_BLACKLIST):
            continue

        if not any(w in text for w in GTT_WHITELIST):
            continue

        # severità
        sev = "MED"
        if any(x in text for x in ["sospes", "interru", "guasto"]):
            sev = "HIGH"
        elif "ritardo" in text:
            sev = "LOW"
        elif "parcheggio" in text:
            sev = "INFO"

        # evidenzia linea
        line = extract_line(title)
        if line:
            title = f"{line} — {title}"

        eid = "gtt:" + sha(title + body)

        if seen_before(eid):
            continue

        msg = format_msg(
            "TRASPORTO PUBBLICO (GTT)",
            sev,
            title,
            body=body,
            link="https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio"
        )

        events.append((eid, msg))

    return events

# --- Comune Torino: Comunicati (cantieri/viabilità) ---
def text_has_any(t: str, kws) -> bool:
    tl = t.lower()
    return any(k in tl for k in kws)

BLACKLIST = [
    "inaugur", "evento", "area cani", "controlli", "locale",
    "cultura", "mostra", "spettacolo", "serata"
]

WHITELIST = [
    "viabilità", "modifiche alla viabilità", "lavori",
    "cantiere", "chiusura", "deviazione", "circolazione",
    "traffico", "strada"
]

def collect_comune_list(url, category, kws, default_sev="MED"):
    html = fetch_text(url)
    soup = BeautifulSoup(html, "html.parser")

    events = []

    for a in soup.select("a"):
        title = a.get_text(" ", strip=True)
        href = a.get("href") or ""

        if not title or len(title) < 10:
            continue

        # ignora link filtro
        if "?" in href:
            continue

        if not href.startswith("/novita/"):
            continue

        title_lower = title.lower()

        # blacklist
        if any(b in title_lower for b in BLACKLIST):
            continue

        # whitelist forte
        if not any(w in title_lower for w in WHITELIST):
            continue

        full = "https://www.comune.torino.it" + href

        sev = default_sev
        if "chius" in title_lower:
            sev = "HIGH"

        eid = f"comune:{category}:" + sha(title + "|" + full)

        if seen_before(eid):
            continue

        msg = format_msg(category, sev, title, link=full)
        events.append((eid, msg))

    return events[:5]

def collect_comune_comunicati():
    # cantieri/viabilità da “comunicati”
    return collect_comune_list(COMUNE_COMUNICATI_URL, "VIABILITÀ / CANTIERI", KW_VIABILITA, default_sev="MED")

def collect_comune_smog():
    # smog da “avvisi” + keyword
    return collect_comune_list(COMUNE_AVVISI_URL, "LIMITAZIONI / SMOG", KW_SMOG, default_sev="LOW")

PRIORITY_ORDER = {
    "CRIT": 0,
    "HIGH": 1,
    "MED": 2,
    "LOW": 3,
    "INFO": 4
}

def classify_event(text):
    t = text.lower()

    if any(x in t for x in ["emergenza", "chiusa", "blocco totale"]):
        return "CRIT"

    if any(x in t for x in ["sospes", "interru", "guasto", "servizio sostitutivo"]):
        return "HIGH"

    if any(x in t for x in ["deviaz", "lavori", "cantiere"]):
        return "MED"

    if any(x in t for x in ["livello", "limitazioni"]):
        return "LOW"

    if any(x in t for x in ["parcheggio"]):
        return "INFO"

    return "INFO"

def shorten(text, max_len=200):
    return text[:max_len] + ("…" if len(text) > max_len else "")
# ========== ENGINE ==========
def run_once():
    all_events = []
    all_events += collect_arpa()
    all_events += collect_gtt()
    all_events += collect_comune_comunicati()
    all_events += collect_comune_smog()

    # ordina per priorità intelligente
    all_events.sort(key=lambda x: PRIORITY_ORDER.get(classify_event(x[1]), 99))

    sent = 0

    for eid, msg in all_events:
        if seen_before(eid):
            continue

        priority = classify_event(msg)

        icon = {
            "CRIT": "🚨",
            "HIGH": "🟠",
            "MED": "🟡",
            "LOW": "🟢",
            "INFO": "ℹ️"
        }[priority]

        final_msg = f"{icon} {(msg)}"

        send_telegram(final_msg)
        mark_seen(eid)
        sent += 1

    cleanup_state()
    save_state(STATE)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] eventi inviati: {sent}")

def main():
    print("🚨 Torino Alert — motore eventi (clean + severità) avviato")
    send_telegram("✅ *Torino Alert* avviato (motore eventi attivo).")

    while True:
        try:
            run_once()
        except Exception as e:
            print("Errore:", e)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()