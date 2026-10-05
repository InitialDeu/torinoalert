"""GTT: avvisi "ultima ora" (pagina raw) e news di servizio (RSS)."""
import feedparser
from bs4 import BeautifulSoup

from ..events import Event
from ..text import norm_key, sha, strip_html, title_with_line

LIVE_URL = "https://www.gtt.to.it/cms/index.php?option=com_gtt&priorita=1&tmpl=raw&view=avvisi"
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


def parse_live(html: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for block in soup.select("div.avviso"):
        heading = block.find(["h4", "h3"])
        title = heading.get_text(" ", strip=True) if heading else ""
        if not title:
            continue
        stamp_el = block.select_one("span.small")
        stamp = stamp_el.get_text(" ", strip=True) if stamp_el else ""
        body = " ".join(p.get_text(" ", strip=True) for p in block.find_all("p"))
        link_el = block.find("a", href=True)

        if not _relevant(f"{title} {body}".lower()):
            continue
        # Data di pubblicazione + titolo: lo stesso avviso ripubblicato un altro
        # giorno ("Luci d'artista") è una nuova notifica, una modifica al testo no.
        events.append(_make_event(
            "gtt-live:" + sha(stamp + "|" + norm_key(title)),
            title, body, link_el["href"] if link_el else "",
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
