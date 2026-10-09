from collections.abc import Callable
from dataclasses import dataclass, field

from .text import normalize_body

SEVERITY_RANK = {"CRIT": 0, "HIGH": 1, "MED": 2, "LOW": 3, "INFO": 4}
# Urgenza scritta, non solo un pallino: si capisce anche nell'anteprima della notifica.
SEVERITY_MARKER = {"CRIT": "🔴 URGENTE", "HIGH": "🟠 IMPORTANTE", "INFO": "ℹ️ INFO"}

# Icona e hashtag per categoria: messaggi di fonti diverse si distinguono a colpo d'occhio
# e si possono filtrare con la ricerca di Telegram.
SOURCE_STYLE = {
    "TRASPORTO PUBBLICO (GTT)": ("🚌", "GTT"),
    "TRASPORTO PUBBLICO REGIONALE": ("🚌", "trasporti"),
    "TRENI (TRENITALIA)": ("🚆", "treni"),
    "FERROVIE (RFI)": ("🚆", "treni"),
    "TRAFFICO": ("🚗", "traffico"),
    "AUTOSTRADA A32 / FREJUS": ("🛣️", "A32"),
    "STRADE PROVINCIALI": ("🛣️", "provinciali"),
    "VIABILITÀ TORINO": ("🚧", "viabilita"),
    "VIABILITÀ / CANTIERI": ("🚧", "viabilita"),
    "ALLERTA METEO": ("⛈️", "allerta"),
    "FIUMI": ("🌊", "fiumi"),
    "TERREMOTO": ("🌍", "terremoto"),
    "SCIOPERI": ("✊", "sciopero"),
    "AEROPORTO CASELLE": ("✈️", "aeroporto"),
    "ACQUA (SMAT)": ("💧", "acqua"),
    "SEMAFORO ANTISMOG": ("🌫️", "smog"),
    "LIMITAZIONI / SMOG": ("🌫️", "smog"),
    "BOLLETTINO CALORE": ("🌡️", "caldo"),
}

TELEGRAM_MAX_LEN = 3800  # limite Telegram 4096, con margine


def _hashtag(text: str) -> str:
    return "#" + "".join(ch for ch in text if ch.isalnum() or ch == "_")


@dataclass(frozen=True)
class Event:
    id: str
    source: str
    severity: str
    title: str
    body: str = ""
    link: str = ""
    # Se valorizzato, un cambio di fingerprint su un ID già visto genera un aggiornamento.
    fingerprint: str = ""
    # Se l'ID non è mai stato visto, registralo senza notificare (es. "allerta VERDE").
    silent_if_new: bool = False
    max_len: int = 900
    # Riga per il riepilogo del mattino; vuota = l'evento non compare nel riepilogo.
    digest_line: str = ""
    # Linee GTT coinvolte quando la fonte le dichiara (GTFS); altrimenti dedotte dal testo.
    lines: tuple[str, ...] = ()
    # Argomento opzionale: vuoto = canale; altrimenti solo a chi lo segue (/segui <argomento>).
    topic: str = ""
    # Disservizio programmato (annunciato in anticipo): finisce nel riepilogo settimanale.
    planned: bool = False
    # Giorni (ISO) citati nel testo, per i riepiloghi; e quando la fonte l'ha pubblicato (epoch).
    days: tuple[str, ...] = ()
    published: int = 0
    # Testo aggiuntivo scaricato solo al momento dell'invio (es. corpo di un articolo).
    enrich: Callable[[], str] | None = field(default=None, compare=False, repr=False)

    def hashtags(self) -> str:
        tags = [SOURCE_STYLE.get(self.source, ("", ""))[1]]
        tags += ["metro" if line == "METRO" else f"linea{line}" for line in self.lines]
        return " ".join(_hashtag(t) for t in tags if t)

    def render(self, update: bool = False, extra: str = "") -> str:
        icon = SOURCE_STYLE.get(self.source, ("📢", ""))[0]
        header = f"{icon} {self.source}"
        if self.severity in SEVERITY_MARKER:
            header += f" · {SEVERITY_MARKER[self.severity]}"
        if update:
            header += " · 🔄 AGGIORNAMENTO"

        parts = [header, self.title.strip()]
        body = normalize_body(self.source, self.title, "\n\n".join(x for x in (self.body, extra) if x), self.max_len)
        if body:
            parts.append(body)
        if self.link:
            parts.append(f"👉 {self.link}")
        tags = self.hashtags()

        msg = "\n\n".join(parts)
        limit = TELEGRAM_MAX_LEN - len(tags) - 2
        if len(msg) > limit:
            msg = msg[:limit].rstrip() + "…"
        return f"{msg}\n\n{tags}" if tags else msg
