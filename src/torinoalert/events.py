from dataclasses import dataclass

from .text import normalize_body

SEVERITY_EMOJI = {"CRIT": "🔴", "HIGH": "🟠", "MED": "🟡", "LOW": "🟢", "INFO": "ℹ️"}
SEVERITY_RANK = {"CRIT": 0, "HIGH": 1, "MED": 2, "LOW": 3, "INFO": 4}

TELEGRAM_MAX_LEN = 3800  # limite Telegram 4096, con margine


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

    def render(self, update: bool = False) -> str:
        emoji = SEVERITY_EMOJI.get(self.severity, "ℹ️")
        header = f"{emoji} {self.source} — TORINO"
        if update:
            header += " · 🔄 AGGIORNAMENTO"

        parts = [header, self.title.strip()]
        body = normalize_body(self.source, self.title, self.body, max_len=self.max_len)
        if body:
            parts.append(body)
        if self.link:
            parts.append(f"👉 {self.link}")

        msg = "\n\n".join(parts)
        if len(msg) > TELEGRAM_MAX_LEN:
            msg = msg[:TELEGRAM_MAX_LEN].rstrip() + "…"
        return msg
