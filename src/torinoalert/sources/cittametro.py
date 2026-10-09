"""Città metropolitana di Torino: modifiche alla viabilità sulle strade provinciali (tabella HTML)."""
import re
from datetime import date

from bs4 import BeautifulSoup

from ..events import Event
from ..text import dates_in_text, norm_key, sha, today_rome

URL = "https://www.cittametropolitana.torino.it/viabilita/percorribilita-strade/modifiche-alla-viabilita-0"
SOURCE = "STRADE PROVINCIALI"

# Il tipo è un'icona: si riconosce dal file.
TYPES = {
    "transito.png": ("Strada chiusa", "MED"),
    "alternato.png": ("Senso unico alternato", "LOW"),
    "camion.png": ("Divieto di transito ai mezzi pesanti", "LOW"),
}


def _dates(text: str, today: date) -> list[date]:
    """Date del periodo: "07/10/2026", "01.10.2026" o "16–17–18 Ottobre 2026"."""
    out = dates_in_text(text, today)
    for d, m, y in re.findall(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", text):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


def parse(html: str, today: date | None = None) -> list[Event]:
    today = today or today_rome()
    soup = BeautifulSoup(html, "html.parser")
    table = next((t for t in soup.find_all("table") if "Numero strada" in t.get_text()), None)
    if table is None:
        raise ValueError("tabella modifiche viabilità non trovata")

    events = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
        published, road, kind_cell, town, km, period, reason = cells[:7]
        img = kind_cell.find("img")
        src = (img.get("src") or "").rsplit("/", 1)[-1] if img else ""
        kind, severity = TYPES.get(src, ((img.get("alt") if img else "") or "Modifica alla viabilità", "LOW"))

        road_t = re.sub(r"\s+", " ", road.get_text(" ", strip=True))
        town_t = town.get_text(" ", strip=True).title()
        period_t = period.get_text(" ", strip=True)
        ends = _dates(period_t, today)
        if ends and max(ends) < today:
            continue  # già terminata

        events.append(Event(
            id="cm-to:" + sha("|".join(norm_key(c.get_text(" ", strip=True)) for c in (published, road, town, km))),
            source=SOURCE,
            topic="provinciali",
            severity=severity,
            title=f"{kind} — {road_t}, {town_t}",
            body="\n".join(x for x in (
                f"Tratto: {km.get_text(' ', strip=True)}",
                f"Quando: {period_t}",
                f"Motivo: {reason.get_text(' ', strip=True)}",
            ) if not x.endswith(": ")),
            link=URL,
            max_len=1200,
        ))
    return events
