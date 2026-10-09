"""GTT: avvisi ufficiali in GTFS-realtime e news di servizio (RSS)."""
import re
import time
from datetime import datetime, timedelta

import feedparser

from .. import gtfsrt
from ..events import Event
from ..text import ROME, dates_in_text, lines_in_text, norm_key, normalize_line, sha, strip_html, title_with_line

ALERTS_URL = "https://percorsieorari.gtt.to.it/das_gtfsrt/alerts.aspx"
NEWS_URL = "https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio?format=feed&type=rss"
LINK = "https://www.gtt.to.it/cms/avvisi-e-informazioni-di-servizio"
SOURCE = "TRASPORTO PUBBLICO (GTT)"

# Comunicazioni promozionali / istituzionali da scartare.
EXCLUDE = [
    "iniziativa", "progetto", "settimana della musica", "personalizzati", "community",
    "engagement", "grafica dedicata", "wrappati", "abbonament", "podcast", "tessera",
]
# Di cosa parla: deve riguardare linee, metro o parcheggi.
SUBJECT = ["linea", "linee", "metro", "parcheggio"]
# Cosa succede: deve essere un evento operativo.
OPERATIONAL = [
    "sospes", "interru", "guasto", "devia", "limit", "ripristin", "impedimento",
    "servizio sostitutivo", "modific", "variazion", "sciopero", "chius", "riprend",
]


RESTORED = ["ripristin", "riprend", "torna", "riapr", "regolare"]
_DAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def _severity_of(text: str) -> str | None:
    if any(x in text for x in RESTORED):
        return "INFO"
    if any(x in text for x in ("sospes", "interru", "guasto", "sciopero")):
        return "HIGH"
    return None


def _severity(title: str, body: str) -> str:
    # Il titolo decide: "ripristinati i percorsi" resta INFO anche se il body
    # racconta la sospensione precedente.
    return _severity_of(title.lower()) or _severity_of(body.lower()) or "MED"


def _relevant(text: str) -> bool:
    return (
        not any(x in text for x in EXCLUDE)
        and any(k in text for k in SUBJECT)
        and any(w in text for w in OPERATIONAL)
    )


def _make_event(event_id: str, title: str, body: str, link: str) -> Event:
    severity = _severity(title, body)
    is_parking = "parcheggio" in title.lower()
    title = title_with_line(title)
    if is_parking:
        title = f"🅿️ {title}"
    return Event(
        id=event_id,
        source=SOURCE,
        severity=severity,
        title=title,
        body=body,
        link=link or LINK,
    )


def route_to_line(route_id: str) -> str | None:
    """route_id GTFS GTT ("17U" urbana, "1432E" extraurbana, "METROU") -> codice linea."""
    rid = (route_id or "").strip().upper()
    if rid.startswith("METRO"):
        return "METRO"
    return normalize_line(rid[:-1] if rid[-1:] in ("U", "E") else rid)


def _fmt_time(ts: int, now: float) -> str:
    dt = datetime.fromtimestamp(ts, ROME)
    year = "" if dt.year == datetime.fromtimestamp(now, ROME).year else f"/{dt.year}"
    return f"{_DAYS[dt.weekday()]} {dt:%d/%m}{year} {dt:%H:%M}"


def _periods_text(periods: list[tuple[int, int]], now: float) -> str:
    parts = []
    for start, end in periods[:3]:
        # GTT mette una fine fittizia (circa un anno) agli avvisi "sino a nuove comunicazioni".
        open_ended = not end or end - now > 180 * 86400
        if start and open_ended:
            parts.append(f"dal {_fmt_time(start, now)} fino a nuova comunicazione")
        elif start:
            parts.append(f"dal {_fmt_time(start, now)} al {_fmt_time(end, now)}")
        elif not open_ended:
            parts.append(f"fino al {_fmt_time(end, now)}")
    if len(periods) > 3:
        parts.append(f"e altri {len(periods) - 3} periodi")
    return ("Avviso valido " + "; ".join(parts)) if parts else ""


# Imprevisti: vanno avvisati subito. Cause GTFS: problema tecnico, incidente, maltempo,
# intervento di polizia, emergenza sanitaria.
UNPLANNED_CAUSES = {3, 6, 8, 11, 12}
UNPLANNED_WORDS = (
    "incident", "guasto", "in panne", "in avaria", "in sosta", "ostru", "intralci", "impedimento",
    "malore", "soccorso", "vigili del fuoco", "intervento delle forze", "allagament", "maltempo",
    "nevicat", "caduta di", "alberi", "seguiranno aggiornamenti", "non preavvisat", "improvvis",
    "cessate esigenze di soccorso",
)
# Programmati: annunciati in anticipo, raccolti nel riepilogo settimanale.
PLANNED_WORDS = (
    "lavori", "cantiere", "manutenzione", "sino a nuove comunicazioni", "fino a nuove comunicazioni",
    "denominata", "concerto", "partita", "mercato", "posa ", "luminarie", "allestimento", "festa",
    "maratona", "gara", "nuovo percorso", "sperimentale", "a partire da",
)


_RANGE_RE = re.compile(r"\b(?:dal|da|dalle ore \S+ di)\b.{0,80}?\b(?:al|a|fino|sino)\b", re.IGNORECASE)


def is_unplanned(alert: gtfsrt.Alert, text: str, days: list) -> bool:
    """
    Le date dichiarate da GTT coincidono con la pubblicazione, non con l'evento:
    si decide su causa, parole e date scritte nel testo.
    """
    if any(w in text for w in UNPLANNED_WORDS):
        return True
    if days or any(w in text for w in PLANNED_WORDS):
        return False
    # La causa dichiarata vale solo senza indizi contrari: GTT a volte lascia "incidente"
    # su deviazioni per lavori che durano da mesi.
    if alert.cause in UNPLANNED_CAUSES:
        return True
    # Nessun indizio: un avviso breve senza date nel testo è quasi sempre un imprevisto.
    start, end = alert.periods[0] if alert.periods else (0, 0)
    return bool(start and end) and end - start <= 12 * 3600


def _alert_severity(alert: gtfsrt.Alert, title: str, lines: set[str], unplanned: bool) -> str:
    # Solo titolo ed effetto: le descrizioni delle deviazioni contengono sempre
    # "riprende regolare percorso" o "fermata sospesa".
    t = title.lower()
    if "ascensor" in t or t.startswith("fermata"):
        return "LOW"  # ascensori e singole fermate: utili, ma non devono far suonare
    if any(x in t for x in RESTORED):
        return "INFO"
    if alert.effect == 1 or any(x in t for x in ("sospes", "interrott", "interruz", "sciopero")):
        return "HIGH" if unplanned or "METRO" in lines else "LOW"
    if unplanned or "METRO" in lines:
        return "MED"  # imprevisto o metro: suona (di giorno)
    return "LOW"


def _topic(alert: gtfsrt.Alert, title: str, lines: set[str], planned: bool = False) -> str:
    """
    Canale: imprevisti urbani, metro, ascensori. Solo a chi li segue: extraurbane,
    singole fermate, programmate (che sul canale arrivano col riepilogo del lunedì).
    """
    kinds = {r.strip().upper()[-1:] for r in alert.routes if r.strip()}
    if kinds == {"E"} or (not kinds and lines and all(x.isdigit() and len(x) == 4 for x in lines)):
        return "extraurbane"  # le linee extraurbane GTT hanno codici a 4 cifre (1432, 2027, ...)
    if not lines and title.lower().startswith("fermata"):
        return "fermate"
    if planned and "METRO" not in lines and "ascensor" not in title.lower():
        return "programmate"
    return ""


def parse_alerts(data: bytes, now: float | None = None) -> list[Event]:
    """Avvisi ufficiali GTT in GTFS-realtime: linee coinvolte, periodi, deviazioni, ascensori metro."""
    now = now if now is not None else time.time()
    events = []
    for alert in gtfsrt.parse_alerts(data):
        if alert.periods and all(end and end < now for _, end in alert.periods):
            continue  # già concluso
        title = re.sub(r"\s+", " ", alert.header).strip() or gtfsrt.EFFECTS.get(alert.effect, "Avviso").capitalize()
        description = alert.description.strip()
        text = f"{title} {description}".lower()
        lines = {line for line in map(route_to_line, alert.routes) if line} | lines_in_text(title)
        elevator = "ascensor" in text
        active_now = not alert.periods or any(
            (not start or start <= now) and (not end or end >= now) for start, end in alert.periods
        )
        today = datetime.fromtimestamp(now, ROME).date()
        mentioned = sorted(set(dates_in_text(f"{title} {description}", today)))
        if len(mentioned) >= 2 and _RANGE_RE.search(text):
            # "da martedì 13 a sabato 24 ottobre": valgono tutti i giorni in mezzo.
            first, last = mentioned[0], min(mentioned[-1], mentioned[0] + timedelta(days=60))
            mentioned = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        days = [d for d in mentioned if today <= d <= today + timedelta(days=60)]
        unplanned = is_unplanned(alert, text, days)
        planned = not unplanned and not elevator
        cause = gtfsrt.CAUSES.get(alert.cause, "")
        body = "\n".join(x for x in (
            _periods_text(alert.periods, now),
            description,
            f"Causa: {cause}" if cause and alert.cause not in (1, 2) and cause not in text else "",
        ) if x)
        if planned:
            digest = title if today in days else ""  # nel riepilogo del giorno in cui avviene
        else:
            digest = title if active_now and not elevator else ""
        events.append(Event(
            id=f"gtt-rt:{alert.id}",
            source=SOURCE,
            severity=_alert_severity(alert, title, lines, unplanned),
            topic=_topic(alert, title, lines, planned),
            title=("🛗 " + title) if elevator else title_with_line(title),
            body=body,
            link=alert.url or LINK,
            # GTT aggiorna lo stesso avviso (nuovo orario, ascensore riparato): si risponde al messaggio.
            fingerprint=sha(norm_key(title) + "|" + norm_key(description) + "|" + repr(alert.periods))[:16],
            lines=tuple(sorted(lines)),
            max_len=1200,
            digest_line=digest,
            planned=planned,
            days=tuple(d.isoformat() for d in days),
            published=alert.periods[0][0] if alert.periods else 0,
        ))
    return events


def parse_news(xml_bytes: bytes) -> list[Event]:
    feed = feedparser.parse(xml_bytes)
    events = []

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        body = strip_html(entry.get("summary") or "")
        text = f"{title} {body}".lower()

        # Solo Torino città/cintura o metro
        if "/torino-e-cintura/" not in link.lower() and "metro" not in text:
            continue
        if not _relevant(text):
            continue

        guid = entry.get("id") or link or norm_key(title)
        events.append(_make_event("gtt-news:" + sha(guid), title, body, link))
    return events
