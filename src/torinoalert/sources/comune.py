"""Comune di Torino: comunicati (viabilità/cantieri) e avvisi (limitazioni smog)."""
from bs4 import BeautifulSoup

from ..events import Event
from ..text import sha, title_has_past_date

BASE = "https://www.comune.torino.it"
COMUNICATI_URL = f"{BASE}/novita/comunicati"
AVVISI_URL = f"{BASE}/novita/avvisi"

VIABILITA_KEYWORDS = ["viabilità", "chius", "deviaz", "lavori", "cantier"]
SMOG_KEYWORDS = ["limitazioni", "livello", "smog"]


def _cards(html: str):
    """(titolo, href) delle notizie in pagina, senza menu né filtri."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select(".node--type-notizia a[href]") or soup.select("a[href]")
    seen = set()
    for a in anchors:
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if len(title) < 10 or "?" in href or not href.startswith("/novita/") or href in seen:
            continue
        seen.add(href)
        yield title, href


def parse_viabilita(html: str) -> list[Event]:
    return [
        Event(
            id="comune:" + sha(href),
            source="VIABILITÀ / CANTIERI",
            severity="MED",
            title=title,
            link=BASE + href,
        )
        for title, href in _cards(html)
        if any(k in title.lower() for k in VIABILITA_KEYWORDS)
    ]


def parse_smog(html: str, today=None) -> list[Event]:
    return [
        Event(
            id="smog:" + sha(href),
            source="LIMITAZIONI / SMOG",
            severity="LOW",
            title=title,
            link=BASE + href,
        )
        for title, href in _cards(html)
        if any(k in title.lower() for k in SMOG_KEYWORDS) and not title_has_past_date(title, today)
    ]
