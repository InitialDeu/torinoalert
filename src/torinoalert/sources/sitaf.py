"""SITAF: programmi mensili di chiusure su A32 Torino-Bardonecchia e traforo del Frejus (T4)."""
from bs4 import BeautifulSoup

from ..events import Event
from ..text import sha

A32_URL = "https://www.sitaf.it/chiusure-programmate-a32/"
T4_URL = "https://www.sitaf.it/chiusure-programmate/"
SOURCE = "AUTOSTRADA A32 / FREJUS"


def parse(html: str, label: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for row in soup.find_all("tr"):
        link = row.find("a", href=True)
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if not link or len(cells) < 2 or not link["href"].lower().endswith(".pdf"):
            continue
        published, document = cells[0], cells[1]
        # La cella del documento ripete la data in testa: "28/09/2026 Chiusure di tratta OTTOBRE 2026".
        title = document.removeprefix(published).strip(" .") or document
        events.append(Event(
            id="sitaf:" + sha(link["href"]),
            source=SOURCE,
            topic="traffico",
            severity="LOW",
            title=f"{label}: {title}",
            body=f"Pubblicato il {published}. I dettagli di date e orari sono nel PDF.",
            link=link["href"],
        ))
    return events
