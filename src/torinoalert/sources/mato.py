"""
Muoversi a Torino (5T, città): modifiche alla viabilità nelle vie di Torino
(sottopassi, manifestazioni, cantieri) e voli di Caselle in tempo reale.
"""
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from ..events import Event
from ..text import ROME, norm_key, sha, today_rome

BASE = "https://www.muoversiatorino.it"
VIABILITA_URL = f"{BASE}/it/modifiche-viabilita/"
HOME_URL = f"{BASE}/it/"
SOURCE_CITY = "VIABILITÀ TORINO"
SOURCE_AIRPORT = "AEROPORTO CASELLE"

# "08/10 CHIUSURA ..." oppure "05-16/10 INTERVENTI ..."
_TITLE_DATES = re.compile(r"^(\d{1,2})(?:-(\d{1,2}))?/(\d{1,2})\b")


def _title_days(title: str, today: date) -> tuple[date, date] | None:
    m = _TITLE_DATES.match(title)
    if not m:
        return None
    month = int(m.group(3))
    year = today.year + (1 if month < today.month - 6 else 0)
    try:
        start = date(year, month, int(m.group(1)))
        end = date(year, month, int(m.group(2) or m.group(1)))
    except ValueError:
        return None
    return start, end


def _city_severity(title: str) -> str:
    t = title.lower()
    if any(x in t for x in ("chiusura", "chiuso", "manifestazion", "divieto", "sospes")):
        return "MED"
    return "LOW"


def parse_viabilita(html: str, today: date | None = None) -> list[Event]:
    today = today or today_rome()
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("span.avviso[data-index]")
    if not items and "avvisi-container" not in html:
        raise ValueError("elenco modifiche viabilità non trovato")

    events = []
    for item in items:
        heading = item.find("h2")
        title = heading.get_text(" ", strip=True) if heading else ""
        if not title:
            continue
        full = item.select_one(".avviso-fulltxt") or item.select_one(".avviso-txt")
        text = re.sub(r"\s+", " ", full.get_text(" ", strip=True)) if full else ""
        days = _title_days(title, today)
        if days and days[1] < today:
            continue  # già concluso
        event_id = item["data-index"]
        events.append(Event(
            id=f"mato:{event_id}",
            source=SOURCE_CITY,
            severity=_city_severity(title),
            title=title.capitalize(),
            body=text,
            link=f"{VIABILITA_URL}?filter={event_id}",
            fingerprint=sha(norm_key(title) + "|" + norm_key(text))[:16],
            digest_line=title.capitalize() if days and days[0] <= today <= days[1] else "",
        ))
    return events


def _minutes(hhmm: str) -> int | None:
    m = re.match(r"(\d{1,2})[:.](\d{2})", hhmm or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def parse_aeroporto(html: str, now: datetime | None = None, min_delay: int = 60) -> list[Event]:
    """Solo voli cancellati o con ritardo di almeno `min_delay` minuti."""
    now = (now or datetime.now(ROME)).astimezone(ROME)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(lambda t: t.name == "h2" and "Aeroporto" in t.get_text())
    if heading is None:
        raise ValueError("sezione aeroporto non trovata")
    section = heading.find_parent(["section", "div"])

    events = []
    for table in section.find_all("table"):
        # Le due tabelle stanno nei tab con id "partenze" e "arrivi".
        arrivals = table.find_parent(id="arrivi") is not None
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) < 4:
                continue
            sched, place, flight, status = cells[:4]
            low = status.lower()
            cancelled = any(x in low for x in ("cancellat", "soppress", "cancelled"))
            expected = re.search(r"\bore\s+(\d{1,2}[:.]\d{2})", low)  # "prevista alle ore 23:25"
            sched_min, new_min = _minutes(sched), _minutes(expected.group(1) if expected else "")
            delay = None
            if sched_min is not None and new_min is not None:
                delay = (new_min - sched_min) % (24 * 60)
            if not cancelled and (delay is None or delay < min_delay or delay > 12 * 60):
                continue

            kind = "Arrivo da" if arrivals else "Volo per"
            what = "cancellato" if cancelled else f"in ritardo di {delay // 60}h{delay % 60:02d}"
            events.append(Event(
                id=f"volo:{now:%Y-%m-%d}:{flight}:{sched}",
                source=SOURCE_AIRPORT,
                severity="MED" if cancelled else "LOW",
                topic="" if cancelled else "aeroporto",  # sul canale solo le cancellazioni
                title=f"✈️ {kind} {place.title()} ({flight}) {what}",
                body=f"Orario previsto {sched} — stato: {status}",
                link=HOME_URL,
                fingerprint="cancellato" if cancelled else "ritardo",
            ))
    return events
