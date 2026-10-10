"""Riepilogo dei disservizi attivi: ogni mattina sul canale e su richiesta con /oggi."""
from collections.abc import Callable
from datetime import date, datetime, timedelta

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


def _day_label(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()][:3]} {d:%d/%m}"


WEEKLY_PIN_KEY = "__meta__:weekly_pin"
TRAIN_SOURCE = "TRENI (TRENITALIA)"


def build_weekly(events: list[Event], now: datetime, max_len: int = 3800) -> str | None:
    """
    Unico messaggio del lunedì, fissato in alto: deviazioni GTT urbane programmate della
    settimana raggruppate per giorno (quelle senza date compaiono la settimana in cui sono
    pubblicate) e lavori sulle linee ferroviarie di Torino attivi in settimana.
    """
    today = now.astimezone(ROME).date()
    week = {today + timedelta(days=i) for i in range(7)}
    since = now.timestamp() - 7 * 86400

    by_day: dict[date, list[str]] = {}
    ongoing: list[str] = []
    trains: list[tuple[date, str]] = []
    for ev in events:
        if not ev.planned:
            continue
        all_days = sorted(date.fromisoformat(x) for x in ev.days)
        in_week = [d for d in all_days if d in week]
        if ev.source == TRAIN_SOURCE:
            if in_week:
                until = f" (fino al {all_days[-1]:%d/%m})" if all_days[-1] > max(week) else ""
                trains.append((in_week[0], f"{ev.title.removeprefix('Lavori — ')}{until}"))
            continue
        if ev.topic != "programmate":
            continue
        if in_week:
            by_day.setdefault(in_week[0], []).append(ev.title)
        elif not ev.days and ev.published >= since:
            ongoing.append(ev.title)
    if not by_day and not ongoing and not trains:
        return None

    parts = [f"📌 LAVORI E DEVIAZIONI DELLA SETTIMANA\n{_day_label(today)} – {_day_label(today + timedelta(days=6))}"]
    if by_day or ongoing:
        gtt = ["🚌 GTT"]
        for day in sorted(by_day):
            gtt.append(f"▪️ {_day_label(day)}\n" + "\n".join(f"• {t}" for t in sorted(set(by_day[day]))))
        if ongoing:
            bullets = "\n".join(f"• {t}" for t in sorted(set(ongoing)))
            gtt.append("▪️ Da questa settimana, fino a nuova comunicazione\n" + bullets)
        parts.append("\n\n".join(gtt))
    if trains:
        rows = [f"• da {_day_label(d)}: {t}" if d > today else f"• {t}" for d, t in sorted(set(trains))]
        parts.append("🚆 TRENI — lavori sulle linee di Torino\n" + "\n".join(rows))
    parts.append("Dettagli in privato: /linea <numero>, /segui programmate, /segui treni")

    text = "\n\n".join(parts)
    if len(text) > max_len:
        text = text[:max_len].rsplit("\n", 1)[0] + "\n… elenco completo con /segui programmate"
    return text


def _post_weekly(text: str, store, notifier) -> None:
    """Pubblica il riepilogo del lunedì, lo fissa in alto e toglie il fissaggio al precedente."""
    msg_id = notifier.send(text, silent=True)
    previous = store.get_many([WEEKLY_PIN_KEY]).get(WEEKLY_PIN_KEY, {}).get("msg")
    try:
        if msg_id:
            notifier.pin(msg_id)
        if previous and previous != msg_id:
            notifier.unpin(previous)
    except Exception as e:  # es. il bot non ha il permesso di fissare messaggi nel canale
        log("weekly_pin_failed", error=repr(e))
    if msg_id:
        store.put(WEEKLY_PIN_KEY, {"msg": int(msg_id)})
    log("weekly_sent", chars=len(text), msg=msg_id)


def _collect(sources, settings: Settings, weather_fn: Callable[[], str]):
    results = collect_all(sources, settings.collect_timeout)
    events = [ev for r in results.values() if isinstance(r, list) for ev in r]
    failed = [name for name, r in results.items() if not isinstance(r, list)]
    try:
        weather = weather_fn()
    except Exception as e:
        log("weather_failed", error=repr(e))
        weather = ""
    return events, weather, failed


def collect_digest(sources, settings: Settings, weather_fn: Callable[[], str], now: datetime | None = None) -> str:
    now = now or datetime.now(ROME)
    events, weather, failed = _collect(sources, settings, weather_fn)
    return build(events, weather, now, failed)


def run_digest(sources, store, notifier, settings: Settings, weather_fn, now: datetime | None = None,
               force: bool = False) -> dict:
    """
    Riepilogo sul canale una volta al giorno all'ora configurata (ora di Roma);
    il lunedì anche il riepilogo settimanale di lavori e deviazioni, fissato in alto.
    """
    now = (now or datetime.now(ROME)).astimezone(ROME)
    if not force and now.hour != settings.digest_hour:
        log("digest_skipped", reason="fuori orario", hour=now.hour)
        return {"status": "skipped"}
    key = digest_key(now.date())
    if not force and store.get_many([key]):
        log("digest_skipped", reason="già inviato")
        return {"status": "skipped"}

    events, weather, failed = _collect(sources, settings, weather_fn)
    text = build(events, weather, now, failed)
    notifier.send(text)
    store.put(key, {"sent_at": int(now.timestamp()), "expires_at": int(now.timestamp()) + 3 * 86400})
    log("digest_sent", chars=len(text))

    weekly = build_weekly(events, now) if now.weekday() == 0 else None
    if weekly:
        _post_weekly(weekly, store, notifier)
    return {"status": "sent", "chars": len(text), "weekly": bool(weekly)}
