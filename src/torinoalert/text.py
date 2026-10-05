import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape

from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo

    ROME = ZoneInfo("Europe/Rome")
except Exception:  # tzdata assente: approssimazione senza ora legale
    ROME = timezone(timedelta(hours=1))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def today_rome() -> date:
    return datetime.now(ROME).date()


def norm_key(text: str) -> str:
    """Forma canonica di un titolo per costruire ID stabili (spazi, maiuscole, punteggiatura)."""
    return re.sub(r"[^0-9a-zà-ù]+", " ", (text or "").lower()).strip()


# ===============================
# DATE NEI TITOLI
# ===============================
MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:°|º)?\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)


def _closest_year(day: int, month: int, today: date) -> int:
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            pass
    if not candidates:
        return today.year
    return min(candidates, key=lambda d: abs((d - today).days)).year


def dates_in_text(text: str, today: date) -> list[date]:
    """
    Date "giorno mese [anno]" nell'ordine in cui compaiono.
    Senza anno: la prima data prende l'anno più vicino a oggi, le successive
    avanzano di un anno se tornano indietro ("dal 15 settembre al 15 aprile").
    """
    found: list[date] = []
    prev = None
    for m in _DATE_RE.finditer(text or ""):
        day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
        if m.group(3):
            year = int(m.group(3))
        elif prev is None:
            year = _closest_year(day, month, today)
        else:
            year = prev.year + (1 if (month, day) < (prev.month, prev.day) else 0)
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        found.append(d)
        prev = d
    return found


def title_has_past_date(title: str, today: date | None = None) -> bool:
    """True se il titolo contiene date e l'ultima è già passata (oggi conta come valida)."""
    today = today or today_rome()
    found = dates_in_text(title, today)
    return bool(found) and max(found) < today


# ===============================
# PULIZIA TESTO
# ===============================
_READ_MORE_RE = re.compile(r"\b(leggi tutto|leggi avviso|continua)\b\.{0,3}", re.IGNORECASE)


_BLOCK_TAGS = ["p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"]


def strip_html(html: str) -> str:
    """Testo da HTML: a capo solo sui blocchi, così <strong>x</strong>. non spezza la frase."""
    soup = BeautifulSoup(html or "", "html.parser")
    for el in soup.find_all(_BLOCK_TAGS):
        if el.name == "li":
            el.insert_before("• ")
        el.insert_after("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in unescape(soup.get_text()).splitlines()]
    return "\n".join(ln for ln in lines if ln)


def normalize_body(source: str, title: str, body: str, max_len: int = 900) -> str:
    """
    Normalizzazione unica per tutti i flussi, stile compatto:
    pulizia HTML, rimozione "Leggi tutto", whitespace, e se troppo lungo
    le prime 1-2 frasi più un'eventuale chiusura (solo GTT/RFI).
    """
    s = (body or "").strip()
    if not s:
        return ""
    if "<" in s and ">" in s:
        s = strip_html(s)
    s = unescape(s)
    s = _READ_MORE_RE.sub("", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s*\n\s*", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()

    if len(s) <= max_len:
        return s

    # Eventi urgenti (sospensioni/guasti): taglio semplice, nessun riassunto.
    low = (title + " " + s).lower()
    if any(k in low for k in ("sospes", "interru", "guasto", "soccorso", "incidente")):
        return s[:max_len].rstrip() + "…"

    one_line = re.sub(r"\s+", " ", s)
    out = " ".join(re.split(r"(?<=[.!?])\s+", one_line)[:2]).strip()

    if source in ("TRASPORTO PUBBLICO (GTT)", "FERROVIE (RFI)"):
        low = one_line.lower()
        if "servizio sostitutivo" in low or "bus sostitutivo" in low:
            out += " Possibile attivazione servizio sostitutivo."
        elif "calendario" in low or re.search(r"\btra il \d{1,2}\b", low):
            out += " Dettagli e calendario nel link."
        elif "lavori" in low:
            out += " Dettagli nel link."

    return out[:max_len].rstrip() + "…"


def extract_line(title: str) -> str | None:
    """Solo 'LINEA <codice>' o metro: niente numeri presi a caso da date e orari."""
    t = (title or "").upper()
    if "METRO" in t:
        return "METROPOLITANA"
    m = re.search(r"\bLINEA\s+([0-9]{1,4}[A-Z]{0,3})\b", t)
    return f"LINEA {m.group(1)}" if m else None


def title_with_line(title: str) -> str:
    """Antepone la linea al titolo se non è già in testa."""
    line = extract_line(title)
    if not line or title.upper().startswith(("LINEA", "LINEE", "METRO")):
        return title
    return f"{line} — {title}"
