"""RFI Infomobilità Piemonte (RSS). RFI aggiorna il titolo dello stesso item."""
import calendar
import time

import feedparser

from ..events import Event
from ..text import mentions, norm_key, sha, strip_html

URL = "https://www.rfi.it/content/rfi/it/news-e-media/infomobilita.rss.updates.piemonte.xml"
SOURCE = "FERROVIE (RFI)"

EVENT_KEYWORDS = [
    "lavori", "manutenzione", "interruzion", "sospes", "rallent", "ritard",
    "guasto", "problema tecnico", "avaria", "sciopero",
    # rientri: utili come chiusura di un evento già notificato
    "regolare", "ripres", "ripristin",
]
TORINO_KEYWORDS = [
    "torino", "porta nuova", "porta susa", "lingotto", "stura", "rebaudengo", "susa",
    "bardonecchia", "chivasso", "ivrea", "pinerolo", "novara", "asti", "alessandria",
]


def classify(text: str) -> str:
    # Prima i rientri: "tornata regolare dopo il guasto" è una buona notizia.
    if any(x in text for x in ("tornata regolare", "ripres", "ripristinat")):
        return "INFO"
    if any(x in text for x in ("interruzion", "sospes")):
        return "HIGH"
    if any(x in text for x in ("guasto", "avaria", "problema tecnico")):
        return "HIGH"
    return "MED"


def parse(xml_bytes: bytes, now: float | None = None) -> list[Event]:
    now = now if now is not None else time.time()
    feed = feedparser.parse(xml_bytes)
    events = []

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        summary = strip_html(entry.get("summary") or "")
        text = f"{title} {summary}".lower()

        if not mentions(text, TORINO_KEYWORDS):
            continue
        if not any(k in text for k in EVENT_KEYWORDS):
            continue
        if title.count(",") >= 4:  # mega elenchi nazionali
            continue

        guid = entry.get("id") or link or norm_key(title)
        published = entry.get("published_parsed")
        recent = published is not None and now - calendar.timegm(published) < 86400
        events.append(Event(
            id="rfi:" + sha(guid),
            source=SOURCE,
            severity=classify(text),
            title=title,
            body=summary,
            link=link,
            fingerprint=sha(norm_key(title) + "|" + norm_key(summary))[:16],
            digest_line=title if recent else "",
        ))
    return events
