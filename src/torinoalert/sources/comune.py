"""Comune di Torino: comunicati (viabilità/cantieri) e avvisi (limitazioni smog)."""
import re
from functools import partial

from bs4 import BeautifulSoup

from ..events import Event
from ..http import fetch_text
from ..text import sha, title_has_past_date

BASE = "https://www.comune.torino.it"
COMUNICATI_URL = f"{BASE}/novita/comunicati"
AVVISI_URL = f"{BASE}/novita/avvisi"

VIABILITA_KEYWORDS = ["viabilità", "chius", "deviaz", "lavori", "cantier", "traffico", "sosta", "ztl",
                      "senso unico", "divieto", "strade", "parcheggi", "transito"]
# Notizie che contengono le parole sopra ma non riguardano chi circola.
VIABILITA_EXCLUDE = ["barriere architettoniche", "edilizia pubblica", "scuola", "riqualificazione dello storico",
                     "mostra", "festival", "concorso", "bando"]
SMOG_KEYWORDS = ["limitazioni", "livello", "smog", "pm10", "semaforo"]


def article_text(url: str, timeout: float = 8.0, max_len: int = 700) -> str:
    """Testo dell'articolo (campo "Testo" del CMS del Comune), scaricato solo al momento dell'invio."""
    soup = BeautifulSoup(fetch_text(url, timeout), "html.parser")
    el = soup.select_one(".field--name-field-testo")
    if el is None:
        return ""
    label = el.select_one(".field__label")
    if label:
        label.decompose()
    text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
    text = re.sub(r"^Testo\s+", "", text)
    return text[:max_len].rsplit(" ", 1)[0] + "…" if len(text) > max_len else text


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
    events = []
    for title, href in _cards(html):
        low = title.lower()
        if not any(k in low for k in VIABILITA_KEYWORDS) or any(x in low for x in VIABILITA_EXCLUDE):
            continue
        events.append(Event(
            id="comune:" + sha(href),
            source="VIABILITÀ / CANTIERI",
            severity="MED",
            title=title,
            link=BASE + href,
            enrich=partial(article_text, BASE + href),
        ))
    return events


def parse_smog(html: str, today=None) -> list[Event]:
    return [
        Event(
            id="smog:" + sha(href),
            source="LIMITAZIONI / SMOG",
            severity="LOW",
            title=title,
            link=BASE + href,
            enrich=partial(article_text, BASE + href),
        )
        for title, href in _cards(html)
        if any(k in title.lower() for k in SMOG_KEYWORDS) and not title_has_past_date(title, today)
    ]
