"""
Trenitalia infomobilità (HTML): avvisi in tempo reale che toccano Torino e
sezione "INFOLAVORI PIEMONTE" (coppie "Linea X" + descrizione).
"""
from datetime import date

from bs4 import BeautifulSoup

from ..events import Event
from ..text import dates_in_text, norm_key, sha, today_rome
from .rfi import TORINO_KEYWORDS

URL = "https://www.trenitalia.com/it/informazioni/Infomobilita/notizie-infomobilita.html"
SOURCE = "TRENI (TRENITALIA)"


def _severity(text: str) -> str:
    if any(x in text for x in ("regolare", "ripres", "ripristin")):
        return "INFO"
    if any(x in text for x in ("sospes", "interrott", "interruzion", "sciopero")):
        return "HIGH"
    return "MED"


def _realtime(item) -> Event | None:
    header = item.select_one(".accordion-header")
    title_el = item.select_one(".infomobility-title") or header
    title = title_el.get_text(" ", strip=True) if title_el else ""
    body_el = item.select_one(".accordion-body")
    body = body_el.get_text("\n", strip=True) if body_el else ""
    text = f"{title} {body}".lower()
    if not any(k in text for k in TORINO_KEYWORDS):
        return None
    return Event(
        id="trenitalia-rt:" + sha(norm_key(title)),
        source=SOURCE,
        severity=_severity(text),
        title=title,
        body=body,
        link=URL,
        fingerprint=sha(norm_key(body))[:16],  # Trenitalia aggiorna il testo dello stesso avviso
        digest_line=title,
    )


def _infolavori(item, today: date) -> list[Event]:
    body = item.select_one(".accordion-body") or item
    events = []
    heading, link, desc = None, URL, []

    def flush():
        if heading and desc:
            text = " ".join(desc)
            if not any(k in f"{heading} {text}".lower() for k in TORINO_KEYWORDS):
                return  # la sezione Piemonte include anche linee lombarde
            on_today = today in dates_in_text(text, today)
            events.append(Event(
                id="trenitalia-lavori:" + sha(norm_key(heading) + "|" + norm_key(text)[:160]),
                source=SOURCE,
                severity="MED" if any(k in text.lower() for k in ("bus", "cancellazion", "sospes")) else "LOW",
                title=f"Lavori — {heading}",
                body=text,
                link=link,
                digest_line=f"{heading}: {text[:120]}" if on_today else "",
            ))

    for p in body.find_all("p"):
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        bold = p.find(["b", "strong"])
        # Un paragrafo che è solo grassetto (spesso un link al PDF) apre una nuova linea.
        if bold and bold.get_text(" ", strip=True) == text:
            flush()
            heading, desc = text, []
            a = p.find("a", href=True)
            link = a["href"] if a else URL
        elif heading:
            desc.append(text)
    flush()
    return events


def parse(html: str, today: date | None = None) -> list[Event]:
    today = today or today_rome()
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for item in soup.select("div.accordion-item"):
        header = item.select_one(".accordion-header")
        label = header.get_text(" ", strip=True).upper() if header else ""
        if "INFOLAVORI" in label:
            if "PIEMONTE" in label:
                events.extend(_infolavori(item, today))
        elif "TRASPORTO REGIONALE" not in label:
            ev = _realtime(item)
            if ev:
                events.append(ev)
    return events
