"""Riepilogo dei disservizi attivi: ogni mattina sul canale e su richiesta con /oggi."""
from collections.abc import Callable
from datetime import datetime

from .config import Settings
from .events import Event
from .log import log
from .runner import collect_all
from .text import MONTHS, ROME

# Ordine delle sezioni: prima ciò che cambia la giornata.
SECTION_ORDER = [
    "ALLERTA METEO", "FIUMI", "SCIOPERI", "TRASPORTO PUBBLICO (GTT)", "TRENI (TRENITALIA)", "FERROVIE (RFI)",
    "VIABILITÀ TORINO", "TRAFFICO", "SEMAFORO ANTISMOG", "BOLLETTINO CALORE",
]
MAX_PER_SECTION = 6
_WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
_MONTH_NAMES = {v: k for k, v in MONTHS.items()}


def digest_key(day) -> str:
    return f"__meta__:digest:{day.isoformat()}"


def build(events: list[Event], weather: str, now: datetime, failed: list[str] | None = None) -> str:
    sections: dict[str, list[str]] = {}
    for ev in events:
        if ev.digest_line:
            lines = sections.setdefault(ev.source, [])
            if ev.digest_line not in lines:
                lines.append(ev.digest_line)

    day = now.astimezone(ROME)
    parts = [f"☀️ TORINO — {_WEEKDAYS[day.weekday()]} {day.day} {_MONTH_NAMES[day.month]}"]
    if weather:
        parts.append(f"🌤 Meteo: {weather}")

    ordered = sorted(sections, key=lambda s: SECTION_ORDER.index(s) if s in SECTION_ORDER else len(SECTION_ORDER))
    for source in ordered:
        lines = sections[source]
        block = [f"▪️ {source}"] + [f"• {x}" for x in lines[:MAX_PER_SECTION]]
        if len(lines) > MAX_PER_SECTION:
            block.append(f"• … e altri {len(lines) - MAX_PER_SECTION}")
        parts.append("\n".join(block))

    if not any(s not in ("ALLERTA METEO", "SEMAFORO ANTISMOG") for s in sections):
        parts.append("✅ Nessun disservizio rilevante segnalato.")
    if failed:
        parts.append(f"⚠️ Fonti non raggiungibili ora: {', '.join(sorted(failed))}")

    text = "\n\n".join(parts)
    return text if len(text) <= 3800 else text[:3800].rsplit("\n", 1)[0] + "\n…"


def collect_digest(sources, settings: Settings, weather_fn: Callable[[], str], now: datetime | None = None) -> str:
    now = now or datetime.now(ROME)
    results = collect_all(sources, settings.collect_timeout)
    events = [ev for r in results.values() if isinstance(r, list) for ev in r]
    failed = [name for name, r in results.items() if not isinstance(r, list)]
    try:
        weather = weather_fn()
    except Exception as e:
        log("weather_failed", error=repr(e))
        weather = ""
    return build(events, weather, now, failed)


def run_digest(sources, store, notifier, settings: Settings, weather_fn, now: datetime | None = None,
               force: bool = False) -> dict:
    """Invia il riepilogo sul canale una volta al giorno, all'ora configurata (ora di Roma)."""
    now = (now or datetime.now(ROME)).astimezone(ROME)
    if not force and now.hour != settings.digest_hour:
        log("digest_skipped", reason="fuori orario", hour=now.hour)
        return {"status": "skipped"}
    key = digest_key(now.date())
    if not force and store.get_many([key]):
        log("digest_skipped", reason="già inviato")
        return {"status": "skipped"}

    text = collect_digest(sources, settings, weather_fn, now)
    notifier.send(text)
    store.put(key, {"sent_at": int(now.timestamp()), "expires_at": int(now.timestamp()) + 3 * 86400})
    log("digest_sent", chars=len(text))
    return {"status": "sent", "chars": len(text)}
