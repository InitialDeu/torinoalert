"""
Trenitalia infomobilità (HTML):
- avvisi su linee dell'area torinese: un messaggio per avviso;
- avvisi su linee lontane e bollettini "INFOTRENI": solo i treni da/per Torino, uno per
  messaggio, senza nominare la linea lontana (altrimenti sembra un problema di Torino);
- "INFOLAVORI PIEMONTE" (coppie "Linea X" + descrizione), raccolti nel riepilogo del lunedì.
"""
import re
from dataclasses import replace
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from ..events import Event
from ..text import ROME, day_span, mentions, norm_key, sha, today_rome
from .rfi import TORINO_KEYWORDS

URL = "https://www.trenitalia.com/it/informazioni/Infomobilita/notizie-infomobilita.html"
SOURCE = "TRENI (TRENITALIA)"
CANCELLED = ("cancellat", "soppress", "non effettua", "limitat", "non ferma", "variazion")


def _severity(text: str) -> str:
    if any(x in text for x in ("regolare", "ripres", "ripristin")):
        return "INFO"
    if any(x in text for x in ("sospes", "interrott", "interruzion", "sciopero")):
        return "HIGH"
    return "MED"


def _line_notice(title: str, body: str) -> Event:
    """Avviso su una linea dell'area torinese (es. "Linea Torino - Milano: circolazione rallentata")."""
    severity = _severity(f"{title} {body}".lower())
    return Event(
        id="trenitalia-rt:" + sha(norm_key(title)),
        source=SOURCE,
        severity=severity,
        topic="" if severity == "HIGH" else "treni",  # sul canale solo sospensioni e interruzioni
        title=title,
        body=body,
        link=URL,
        fingerprint=sha(norm_key(body))[:16],  # Trenitalia aggiorna il testo dello stesso avviso
        digest_line=title,
        close_notice=True,
    )


def _general_reason(body_el) -> str:
    """Prima frase che spiega il problema ("La circolazione è rallentata per un guasto ...")."""
    for p in body_el.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if p.find("a") or not text or text.lower().startswith("aggiornamento"):
            continue
        if re.search(r"\bper\b|\bcausa\b|\bdovut", text, re.IGNORECASE):
            return text
    return ""


def _delay(body_text: str) -> str:
    m = re.search(r"(superiore a|oltre|fino a)\s+(\d+)\s+minuti", body_text, re.IGNORECASE)
    if not m:
        return ""
    return f"ritardo {'fino a' if m.group(1).lower() == 'fino a' else 'oltre'} {m.group(2)} minuti"


def _torino_trains(body_el, today: date) -> list[Event]:
    """Un evento per ogni treno da/per Torino citato nell'avviso (link a ViaggiaTreno)."""
    body_text = body_el.get_text(" ", strip=True)
    reason = _general_reason(body_el)
    delay = _delay(body_text)
    events = []
    for a in body_el.select("a[href*='cercaTreno']"):
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if "torino" not in label.lower():
            continue
        number = re.search(r"\b(\d{2,5})\b", label)
        if not number:
            continue
        query = parse_qs(urlparse(a["href"]).query)
        try:
            day = datetime.fromtimestamp(int(query["datapartenza"][0]) / 1000, ROME).date()
        except (KeyError, ValueError, IndexError):
            day = today

        # Dopo il nome del treno c'è il dettaglio specifico (": il treno viaggia in ritardo per...").
        paragraph = a.find_parent("p")
        own = paragraph.get_text(" ", strip=True) if paragraph else ""
        own = own.split(label, 1)[-1] if label in own else ""
        own = re.sub(r"^[\s.:;,-]*(del \d{1,2} \w+)?[\s.:;,-]*", "", own).split("•")[0].strip()
        detail = own or reason
        text = f"{detail} {delay}".lower()
        cancelled = any(x in text for x in CANCELLED)
        status = "variazioni di percorso o cancellazione" if cancelled else (delay or "in ritardo")

        events.append(Event(
            id=f"treno:{number.group(1)}:{day.isoformat()}",
            source=SOURCE,
            severity="MED" if cancelled else "LOW",
            title=f"🚄 {label.replace(' - ', ' → ')}: {status}",
            body=detail,
            link=a["href"],
            fingerprint=sha(norm_key(detail) + "|" + status)[:16],
        ))
    return events


def _infolavori(item, today: date) -> list[Event]:
    body = item.select_one(".accordion-body") or item
    events = []
    heading, link, desc = None, URL, []

    def flush():
        if heading and desc:
            text = " ".join(desc)
            if not mentions(f"{heading} {text}", TORINO_KEYWORDS):
                return  # la sezione Piemonte include anche linee lombarde
            days = day_span(text, today)
            events.append(Event(
                id="trenitalia-lavori:" + sha(norm_key(heading) + "|" + norm_key(text)[:160]),
                source=SOURCE,
                severity="MED" if any(k in text.lower() for k in ("bus", "cancellazion", "sospes")) else "LOW",
                title=f"Lavori — {heading}",
                topic="treni",
                body=text,
                link=link,
                digest_line=f"{heading}: {text[:120]}" if today in days else "",
                planned=True,
                days=tuple(d.isoformat() for d in days),
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


_RANK = {"HIGH": 0, "MED": 1, "LOW": 2, "INFO": 3}


def _merge_trains(events: list[Event]) -> list[Event]:
    """Lo stesso treno in più avvisi (ritardo + fermate saltate) diventa un solo messaggio."""
    merged: dict[str, Event] = {}
    for ev in events:
        prev = merged.get(ev.id)
        if prev is None:
            merged[ev.id] = ev
            continue
        main = prev if _RANK[prev.severity] <= _RANK[ev.severity] else ev
        body = "\n\n".join(dict.fromkeys(x for x in (main.body, prev.body, ev.body) if x))
        merged[ev.id] = replace(main, body=body, fingerprint=sha(norm_key(body))[:16])
    return list(merged.values())


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
            continue
        if "TRASPORTO REGIONALE" in label:
            continue
        title_el = item.select_one(".infomobility-title") or header
        title = title_el.get_text(" ", strip=True) if title_el else ""
        body_el = item.select_one(".accordion-body")
        if body_el is None:
            continue
        if mentions(title, TORINO_KEYWORDS):
            events.append(_line_notice(title, body_el.get_text("\n", strip=True)))
        else:
            events.extend(_torino_trains(body_el, today))
    return _merge_trains(events)
