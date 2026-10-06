"""Allerta meteo ARPA Piemonte (feed CAP 1.2, un blocco <info> per zona)."""
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from ..events import Event
from ..text import MONTHS, ROME

URL = "https://www.arpa.piemonte.it/export/xmlcap/allerta.xml"
LINK = "https://www.arpa.piemonte.it"
SOURCE = "ALLERTA METEO"

LEVELS = {"GIALLO": 1, "ARANCIONE": 2, "ROSSO": 3}  # VERDE / BIANCO = nessuna allerta
LEVEL_LABEL = {1: "GIALLA", 2: "ARANCIONE", 3: "ROSSA"}
LEVEL_SEVERITY = {1: "MED", 2: "HIGH", 3: "CRIT"}
RISKS = {
    "IDRAULICO": "Idraulico",
    "IDROGEOLOGICO": "Idrogeologico",
    "TEMPORALI": "Temporali",
    "NEVE": "Neve",
    "VALANGHE": "Valanghe",
}
# Suffisso del parametro -> giorni dopo l'onset del bollettino.
PERIOD_OFFSET = {"1224": 0, "2436": 1}
ZONE_NAMES = {"Piem-L": "Pianura torinese e colline"}

_MONTH_NAMES = {v: k for k, v in MONTHS.items()}


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _zone_label(zone: str) -> str:
    code = zone.split("-")[-1]
    name = ZONE_NAMES.get(zone)
    return f"zona {code} ({name})" if name else f"zona {code}"


def parse(xml_bytes: bytes, zones: tuple[str, ...], now: datetime | None = None) -> list[Event]:
    now = now or datetime.now(UTC)
    root = ET.fromstring(xml_bytes)
    bulletin = (root.findtext("{*}identifier") or "").strip()

    events = []
    for info in root.findall("{*}info"):
        zone = (info.findtext("{*}area/{*}areaDesc") or "").strip()
        if zone not in zones:
            continue

        expires = _parse_dt(info.findtext("{*}expires"))
        if expires and expires < now:
            continue  # bollettino scaduto: nessuna informazione affidabile
        onset = _parse_dt(info.findtext("{*}onset")) or now
        onset_day = onset.astimezone(ROME).date()

        alerts = []  # (giorno, rischio, livello)
        effects = ""
        for p in info.findall("{*}parameter"):
            name = (p.findtext("{*}valueName") or "").strip().upper()
            value = (p.findtext("{*}value") or "").strip().upper()
            if name == "EFFETTI SUL TERRITORIO":
                effects = (p.findtext("{*}value") or "").strip().strip("-").strip()
                continue
            risk, _, period = name.rpartition("_")
            if risk in RISKS and period in PERIOD_OFFSET and value in LEVELS:
                day = onset_day + timedelta(days=PERIOD_OFFSET[period])
                alerts.append((day, risk, LEVELS[value]))

        events.append(_zone_event(zone, alerts, effects, bulletin))
    return events


def _zone_event(zone, alerts, effects, bulletin) -> Event:
    # Il fingerprint usa il livello massimo per rischio, non le date: così il
    # passaggio "domani" -> "oggi" di un'allerta invariata non rinotifica.
    max_by_risk: dict[str, int] = {}
    for _, risk, level in alerts:
        max_by_risk[risk] = max(level, max_by_risk.get(risk, 0))
    fingerprint = ";".join(f"{r}={lvl}" for r, lvl in sorted(max_by_risk.items())) or "VERDE"

    if not alerts:
        return Event(
            id=f"arpa:{zone}",
            source=SOURCE,
            severity="INFO",
            title=f"Allerta rientrata — {_zone_label(zone)}: nessuna criticità (VERDE)",
            body=f"Bollettino n. {bulletin}" if bulletin else "",
            link=LINK,
            fingerprint=fingerprint,
            silent_if_new=True,
            digest_line=f"Nessuna allerta meteo — {_zone_label(zone)}",
        )

    top = max(level for _, _, level in alerts)
    lines = [
        f"• {d.day} {_MONTH_NAMES[d.month]}: {RISKS[risk]} — {LEVEL_LABEL[level]}"
        for d, risk, level in sorted(alerts, key=lambda a: (a[0], -a[2], a[1]))
    ]
    if effects:
        lines.append(f"\nEffetti sul territorio: {effects}")
    if bulletin:
        lines.append(f"\nBollettino n. {bulletin}")

    return Event(
        id=f"arpa:{zone}",
        source=SOURCE,
        severity=LEVEL_SEVERITY[top],
        title=f"Allerta {LEVEL_LABEL[top]} — {_zone_label(zone)}",
        body="\n".join(lines),
        link=LINK,
        fingerprint=fingerprint,
    )
