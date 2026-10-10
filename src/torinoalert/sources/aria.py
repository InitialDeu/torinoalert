"""ARPA Piemonte qualità dell'aria: semaforo antismog e bollettino calore per Torino (JSON)."""
import json
from datetime import date

from ..events import Event
from ..text import today_rome

SEMAFORO_URL = "https://staticaria.arpa.piemonte.it/aria_production/semaforo.json"
CALDO_URL = "https://staticaria.arpa.piemonte.it/sc05/caldo.json"
LINK_SEMAFORO = "https://webgis.arpa.piemonte.it/protocollo_aria_webapp/index.html?page=semaforo"
LINK_CALDO = "https://www.arpa.piemonte.it/rischi_naturali/boll/bollettino_calore_comune_torino.pdf"

TORINO_ISTAT = "001272"
# Zona del bollettino calore con il bollettino sanitario dedicato alla città di Torino.
TORINO_HEAT_ZONE = "59"

SEMAFORO = {
    0: ("verde", "🟢", "solo limitazioni permanenti", "INFO"),
    1: ("arancio", "🟠", "limitazioni di livello 1 (PM10 oltre 50 µg/m³ per 3 giorni)", "MED"),
    2: ("rosso", "🔴", "limitazioni di livello 2", "HIGH"),
}
CALDO = {
    0: ("nessun disagio", "INFO"),
    1: ("debole disagio", "LOW"),
    2: ("moderato disagio", "MED"),
    3: ("forte disagio — ondata di calore", "HIGH"),
}


def _level_text(level: int, day: str) -> str:
    if level not in SEMAFORO:
        return f"{day}: non disponibile"
    name, emoji, desc, _ = SEMAFORO[level]
    return f"{day}: {emoji} livello {level} ({name}) — {desc}"


def parse_semaforo(data: bytes) -> list[Event]:
    doc = json.loads(data)
    torino = next((c for c in doc.get("C", []) if c.get("I") == TORINO_ISTAT), None)
    if torino is None:
        raise ValueError("Torino non presente nel semaforo")
    today_lvl, tomorrow_lvl = int(torino.get("LO", -999)), int(torino.get("LD", -999))
    worst = max(today_lvl, tomorrow_lvl)
    body = "\n".join((
        _level_text(today_lvl, f"Oggi {doc.get('DO', '')}"),
        _level_text(tomorrow_lvl, f"Domani {doc.get('DD', '')}"),
    ))
    return [Event(
        id="semaforo:torino",
        source="SEMAFORO ANTISMOG",
        severity=SEMAFORO.get(worst, SEMAFORO[0])[3],
        title=f"Semaforo antismog Torino: oggi livello {today_lvl if today_lvl >= 0 else 'n/d'}",
        body=body,
        link=torino.get("U") or LINK_SEMAFORO,
        fingerprint=f"{today_lvl}/{tomorrow_lvl}",
        state=True,
        silent_if_new=worst <= 0,
        digest_line=_level_text(today_lvl, "Semaforo antismog oggi"),
    )]


def parse_caldo(data: bytes, today: date | None = None) -> list[Event]:
    today = today or today_rome()
    doc = json.loads(data)
    try:
        next_update = date.fromisoformat(doc.get("prossimo_aggiornamento", ""))
    except ValueError:
        next_update = None
    if not next_update or next_update < today:
        return []  # bollettino fuori stagione o non aggiornato

    levels = [
        int(by_zone[TORINO_HEAT_ZONE].get("0", 0))
        for _, by_zone in sorted((doc.get("COD_COLORE") or {}).items(), key=lambda kv: int(kv[0]))
        if TORINO_HEAT_ZONE in by_zone
    ]
    if not levels:
        return []
    worst = max(levels)
    label, severity = CALDO.get(worst, CALDO[0])
    return [Event(
        id="caldo:torino",
        source="BOLLETTINO CALORE",
        severity=severity,
        title=f"Calore Torino: {label}",
        body=(
            f"Livelli previsti nei prossimi giorni: {', '.join(str(x) for x in levels)} "
            "(0 = nessun disagio, 3 = ondata di calore)."
        ),
        link=LINK_CALDO,
        fingerprint=str(worst),
        state=True,
        silent_if_new=worst == 0,
        digest_line=f"Calore: {label}" if worst > 0 else "",
    )]
