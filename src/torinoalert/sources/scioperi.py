"""Scioperi dal Ministero dei Trasporti (RSS): proclamazione + promemoria il giorno prima."""
import re
from datetime import date, timedelta

import feedparser

from ..events import Event
from ..text import parse_it_date, strip_html, today_rome

URL = "https://scioperi.mit.gov.it/mit2/public/scioperi/rss"
LINK = "https://scioperi.mit.gov.it/mit2/public/scioperi"
SOURCE = "SCIOPERI"

# Settori che toccano chi si muove a/da Torino.
SECTORS = ("trasporto pubblico locale", "ferroviario", "aereo", "generale", "plurisettoriale", "autostrade", "taxi")


def _fields(entry) -> dict[str, str]:
    """"Chiave: valore" da titolo e descrizione (separati da " - " e <br/>)."""
    text = (entry.get("title") or "") + "\n" + strip_html(entry.get("summary") or "")
    out = {}
    for part in re.split(r"\n| - ", text):
        key, sep, value = part.partition(":")
        if sep:
            out[key.strip().lower()] = re.sub(r"\s+", " ", value).strip()
    return out


def _relevant(f: dict[str, str]) -> bool:
    sector = f.get("settore", "").lower()
    region = f.get("regione", "").lower()
    province = f.get("provincia", "").lower()
    scope = f.get("rilevanza", "").lower()
    if not any(s in sector for s in SECTORS):
        return False
    if scope == "nazionale" or region in ("", "tutte"):
        return True
    return "piemonte" in region and province in ("tutte", "torino", "")


def _severity(f: dict[str, str]) -> str:
    sector = f.get("settore", "").lower()
    if "trasporto pubblico locale" in sector or "generale" in sector:
        return "HIGH"
    if "ferroviario" in sector:
        return "HIGH" if f.get("rilevanza", "").lower() in ("nazionale", "regionale") else "MED"
    return "MED"


def _when(start: date, end: date | None) -> str:
    if not end or end == start:
        return start.strftime("%d/%m/%Y")
    return f"dal {start:%d/%m/%Y} al {end:%d/%m/%Y}"


def parse(xml_bytes: bytes, today: date | None = None) -> list[Event]:
    today = today or today_rome()
    events = []
    for entry in feedparser.parse(xml_bytes).entries:
        f = _fields(entry)
        start = parse_it_date(f.get("data inizio", ""))
        end = parse_it_date(f.get("data fine", "")) or start
        if not start or (end and end < today) or not _relevant(f):
            continue

        guid = entry.get("id") or entry.get("link") or entry.get("title")
        sector = f.get("settore", "?")
        where = "nazionale" if f.get("rilevanza", "").lower() == "nazionale" else f.get("regione", "")
        title = f"Sciopero {sector.lower()} ({where}) — {_when(start, end)}"
        body = "\n".join(
            f"{label}: {f[key]}"
            for key, label in (
                ("modalità", "Modalità"),
                ("categoria interessata", "Categoria"),
                ("sindacati", "Sindacati"),
                ("data proclamazione", "Proclamato il"),
            )
            if f.get(key)
        )
        severity = _severity(f)
        soon = today <= start <= today + timedelta(days=2)
        digest = f"{start:%d/%m} {sector} ({where}): {f.get('modalità', '')}".strip() if soon else ""

        events.append(Event(
            id=f"sciopero:{guid}", source=SOURCE, severity=severity, title=title,
            body=body, link=LINK, max_len=1200, digest_line=digest,
        ))
        # Promemoria: un secondo evento che nasce solo il giorno prima (o il giorno stesso).
        if start - timedelta(days=1) <= today <= start:
            label = "Domani" if start > today else "Oggi"
            events.append(Event(
                id=f"sciopero-promemoria:{guid}", source=SOURCE, severity=severity,
                title=f"⏰ {label} sciopero {sector.lower()} ({where})",
                body=body, link=LINK, max_len=1200,
            ))
    return events
