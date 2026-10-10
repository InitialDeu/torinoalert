"""
Un'esecuzione completa:
raccolta parallela -> dedup (con fingerprint e TTL rinnovato) -> bootstrap
silenzioso delle fonti nuove -> consegna ordinata per gravità:
  - eventi senza argomento: canale (tetto, 429, notte silenziosa, risposte al
    messaggio originale) + privato a chi segue le linee GTT citate;
  - eventi con argomento: solo in privato a chi segue l'argomento o le linee;
  - aggiornamenti: subito se cambia la gravità, altrimenti al massimo uno ogni
    `update_min_interval_minutes`;
-> salute delle fonti con avviso admin.
"""
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

from . import subscriptions
from .config import Settings
from .events import SEVERITY_RANK, Event, closing_text
from .log import log
from .sources import gtt
from .telegram import TelegramError
from .text import ROME, lines_in_text

Sources = dict[str, Callable[[], list[Event]]]


def bootstrap_key(source: str) -> str:
    return f"__meta__:bootstrap:{source}"


def health_key(source: str) -> str:
    return f"__meta__:health:{source}"


def open_key(source: str) -> str:
    """Avvisi pubblicati sul canale e non ancora risolti, per fonte."""
    return f"__meta__:open:{source}"


# Giri consecutivi in cui un avviso deve mancare prima di dichiararlo risolto
# (un feed che "perde" un avviso per un giro non deve generare falsi "risolto").
RESOLVE_AFTER_MISSES = 2


def collect_all(sources: Sources, timeout: float) -> dict[str, list[Event] | Exception]:
    """Scarica tutte le fonti in parallelo: una fonte lenta non blocca le altre."""
    results: dict[str, list[Event] | Exception] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, len(sources)))
    futures = {pool.submit(fn): name for name, fn in sources.items()}
    try:
        for fut in as_completed(futures, timeout=timeout):
            try:
                results[futures[fut]] = fut.result()
            except Exception as e:
                results[futures[fut]] = e
    except TimeoutError:
        for name in futures.values():
            results.setdefault(name, TimeoutError(f"raccolta oltre {timeout}s"))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def is_silent(severity: str, now: float, settings: Settings) -> bool:
    """INFO/LOW non suonano mai; di notte suona solo CRIT."""
    if severity in ("INFO", "LOW"):
        return True
    hour = datetime.fromtimestamp(now, ROME).hour
    start, end = settings.quiet_start, settings.quiet_end
    night = (hour >= start or hour < end) if start > end else (start <= hour < end)
    return night and severity != "CRIT"


def _record(ev: Event, now: int, ttl: int, prev: dict | None = None, msg: int | None = None,
            delivered: bool = False) -> dict:
    """Stato dell'evento: fingerprint, scadenza, messaggio sul canale, ultima consegna."""
    prev = prev or {}
    item = {"fp": ev.fingerprint, "expires_at": now + ttl, "sev": ev.severity,
            "sent_at": now if delivered else int(prev.get("sent_at", 0))}
    msg = msg or prev.get("msg")
    if msg:
        item["msg"] = int(msg)
    return item


def _event_lines(ev: Event) -> set[str]:
    if ev.source != gtt.SOURCE:
        return set()
    return set(ev.lines) or lines_in_text(f"{ev.title} {ev.body}")


@dataclass
class _Outcome:
    channel: int = 0
    dm: int = 0
    private_only: int = 0
    dropped: int = 0
    failed: int = 0
    throttled: int = 0
    handled: set = field(default_factory=set)
    opened: list = field(default_factory=list)  # (evento, message_id) da seguire fino alla risoluzione


def run(
    sources: Sources,
    store,
    notifier,
    settings: Settings,
    *,
    now: float | None = None,
    deadline: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    started = clock()
    now = int(now if now is not None else time.time())
    ttl = settings.dedup_ttl_seconds

    results = collect_all(sources, settings.collect_timeout)

    # Eventi per fonte, senza ID duplicati (BatchGetItem rifiuta chiavi ripetute).
    events_by_source: dict[str, list[Event]] = {}
    ids: set[str] = set()
    for name in sources:
        res = results.get(name)
        if isinstance(res, list):
            events_by_source[name] = [ev for ev in res if not (ev.id in ids or ids.add(ev.id))]

    records = store.get_many(
        list(ids) + [bootstrap_key(n) for n in sources] + [health_key(n) for n in sources]
        + [open_key(n) for n in sources]
    )

    pending: list[tuple[Event, dict | None]] = []  # (evento, record esistente se è un aggiornamento)
    silenced = refreshed = absorbed = 0
    for name, events in events_by_source.items():
        bootstrapping = settings.bootstrap_silent and bootstrap_key(name) not in records
        for ev in events:
            rec = records.get(ev.id)
            if rec is None:
                if bootstrapping or ev.silent_if_new:
                    store.put(ev.id, _record(ev, now, ttl))
                    silenced += 1
                else:
                    pending.append((ev, None))
            elif ev.fingerprint and rec.get("fp") != ev.fingerprint:
                if _delivered(rec):
                    pending.append((ev, rec))  # aggiornamento di qualcosa che qualcuno ha visto
                elif ev.state or rec.get("sev") not in (None, ev.severity):
                    pending.append((ev, None))  # cambio di stato o di gravità: nuovo messaggio
                else:
                    # Mai mostrato a nessuno (registrato al bootstrap, o in un argomento senza
                    # iscritti): niente "AGGIORNAMENTO", si aggiorna lo stato in silenzio.
                    store.put(ev.id, _record(ev, now, ttl, prev=rec))
                    absorbed += 1
            elif int(rec.get("expires_at", 0)) - now < ttl // 2:
                # Ancora pubblicato: rinnova il TTL, scadrà solo dopo che sparisce dalla fonte.
                store.put(ev.id, _record(ev, now, ttl, prev=rec))
                refreshed += 1
        if bootstrapping:
            store.put(bootstrap_key(name), {"created_at": now})
            log("source_bootstrapped", source=name, events=len(events))

    _update_health(sources, results, records, store, notifier, settings, now)

    pending.sort(key=lambda p: SEVERITY_RANK.get(p[0].severity, len(SEVERITY_RANK)))
    out = _deliver_all(pending, store, notifier, settings, now, ttl, deadline, sleep, clock)
    resolved = _close_resolved(events_by_source, records, out.opened, store, notifier, settings, sleep)
    store.flush()

    summary = {
        "status": "ok",
        "sent": out.channel,
        "resolved": resolved,
        "dm": out.dm,
        "private_only": out.private_only,
        "throttled": out.throttled,
        "pending": len(pending) - len(out.handled),
        "dropped": out.dropped,
        "failed": out.failed,
        "silenced": silenced,
        "refreshed": refreshed,
        "absorbed": absorbed,
        "events": len(ids),
        "per_source": {
            n: (len(r) if isinstance(r, list) else f"error: {r}") for n, r in results.items()
        },
        "duration": round(clock() - started, 2),
    }
    log("run_summary", **summary)
    return summary


def _delivered(rec: dict) -> bool:
    """L'evento è arrivato a qualcuno (canale o almeno un iscritto)?"""
    return bool(rec.get("msg")) or int(rec.get("sent_at", 0)) > 0


def _update_due(ev: Event, rec: dict, now: int, settings: Settings) -> bool:
    """Un aggiornamento parte subito se cambia la gravità, altrimenti non più di uno ogni N minuti."""
    if rec.get("sev") != ev.severity:
        return True
    return now - int(rec.get("sent_at", 0)) >= settings.update_min_interval_minutes * 60


def _deliver_all(pending, store, notifier, settings, now, ttl, deadline, sleep, clock) -> _Outcome:
    out = _Outcome()
    # Destinatari privati letti una volta sola per tutto il giro.
    lines_needed = set().union(*(_event_lines(ev) for ev, _ in pending)) if pending else set()
    topics_needed = {ev.topic for ev, _ in pending if ev.topic}
    by_line, by_topic = subscriptions.recipients(store, lines_needed, topics_needed)

    for ev, rec in pending:
        if deadline is not None and clock() > deadline:
            log("send_deadline_reached")
            break
        is_update = rec is not None
        if is_update and not _update_due(ev, rec, now, settings):
            out.throttled += 1
            continue

        # Un evento già pubblicato sul canale si aggiorna lì, in risposta al messaggio originale.
        in_channel = not ev.topic or bool(is_update and rec.get("msg"))
        private = {c for line in _event_lines(ev) for c in by_line.get(line, [])}
        if ev.topic and not in_channel:
            private |= set(by_topic.get(ev.topic, []))

        if in_channel and out.channel >= settings.max_sends_per_run:
            continue  # resta in sospeso: prossimo giro
        if private and out.dm + len(private) > settings.max_dm_per_run and not in_channel:
            continue

        text = _text(ev, is_update)
        silent = is_silent(ev.severity, now, settings)
        msg_id = None
        if in_channel:
            if out.channel or out.failed:
                sleep(settings.send_interval)
            kwargs = {"silent": silent}
            if is_update and rec.get("msg"):
                kwargs["reply_to"] = int(rec["msg"])
            outcome, msg_id = _deliver(notifier, text, kwargs, settings, deadline, sleep, clock)
            if outcome == "drop":
                # Telegram rifiuta il messaggio (400): non riprovare all'infinito.
                store.put(ev.id, _record(ev, now, ttl, prev=rec, delivered=True))
                out.dropped += 1
                out.handled.add(ev.id)
                continue
            if outcome == "stop":
                break
            if outcome != "ok":
                out.failed += 1  # non marcato: si riprova alla prossima esecuzione
                continue
            out.channel += 1
            if ev.close_notice and msg_id:
                out.opened.append((ev, msg_id))
        else:
            out.private_only += 1

        reached = _send_private(notifier, private, text, silent, settings, out.dm)
        out.dm += reached
        # "Consegnato" solo se l'ha visto qualcuno: un argomento senza iscritti non genera aggiornamenti.
        store.put(ev.id, _record(ev, now, ttl, prev=rec, msg=msg_id, delivered=in_channel or reached > 0))
        out.handled.add(ev.id)
        log("sent", event_id=ev.id, severity=ev.severity, update=is_update, silent=silent,
            channel=in_channel, topic=ev.topic, private=len(private))
    return out


def _close_resolved(events_by_source, records, opened, store, notifier, settings, sleep) -> int:
    """
    Per ogni fonte letta con successo: gli avvisi pubblicati sul canale che non compaiono
    più per RESOLVE_AFTER_MISSES giri ricevono un "✅ RISOLTO" in risposta al messaggio.
    """
    just_opened = {ev.id: msg for ev, msg in opened}
    resolved = 0
    for name, events in events_by_source.items():
        if not events:
            continue  # fonte vuota: probabilmente un problema del feed, non "tutto risolto"
        key = open_key(name)
        tracked = json.loads(records.get(key, {}).get("items") or "{}")
        changed = False
        for ev in events:
            if ev.id in just_opened:
                tracked[ev.id] = {"m": int(just_opened[ev.id]), "t": ev.title, "s": ev.source, "x": 0}
                changed = True
        current = {ev.id for ev in events}
        for event_id, item in list(tracked.items()):
            if event_id in current:
                if item.get("x"):
                    item["x"], changed = 0, True
                continue
            item["x"] = int(item.get("x", 0)) + 1
            changed = True
            if item["x"] < RESOLVE_AFTER_MISSES:
                continue
            if resolved:
                sleep(settings.send_interval)
            try:
                notifier.send(closing_text(item.get("s", ""), item["t"]), silent=True, reply_to=item["m"])
            except Exception as e:
                log("resolve_failed", event_id=event_id, error=repr(e))
                continue  # si riprova al prossimo giro
            del tracked[event_id]
            resolved += 1
            log("resolved", event_id=event_id)
        if changed:
            store.put(key, {"items": json.dumps(tracked, ensure_ascii=False)})
    return resolved


def _text(ev: Event, is_update: bool) -> str:
    extra = ""
    if ev.enrich and not is_update:
        try:
            extra = ev.enrich()
        except Exception as e:
            log("enrich_failed", event_id=ev.id, error=repr(e))
    return ev.render(update=is_update, extra=extra)


def _send_private(notifier, chats, text, silent, settings, already: int) -> int:
    count = 0
    for chat in sorted(chats):
        if already + count >= settings.max_dm_per_run:
            log("dm_cap_reached", sent=already + count)
            break
        try:
            notifier.send(text, chat_id=chat, silent=silent)
            count += 1
        except Exception as e:
            # 403: l'utente ha bloccato il bot; si continua con gli altri.
            log("dm_failed", chat=chat, error=repr(e))
    return count


def _deliver(notifier, text, kwargs, settings, deadline, sleep, clock) -> tuple[str, int | None]:
    for attempt in range(2):
        try:
            return "ok", notifier.send(text, **kwargs)
        except TelegramError as e:
            if e.retry_after is None:
                log("send_failed", status=e.status, error=e.description)
                return ("drop" if e.status == 400 else "fail"), None
            wait = float(e.retry_after)
            too_late = deadline is not None and clock() + wait > deadline
            if attempt or wait > settings.max_retry_wait or too_late:
                log("send_rate_limited", retry_after=wait)
                return "stop", None
            sleep(wait)
        except Exception as e:
            log("send_failed", error=repr(e))
            return "fail", None
    return "stop", None


def _admin(notifier, text: str) -> None:
    try:
        notifier.send_admin(text)
    except Exception as e:
        log("admin_send_failed", error=repr(e))


def _update_health(sources, results, records, store, notifier, settings, now) -> None:
    for name in sources:
        key = health_key(name)
        rec = records.get(key, {})
        failures = int(rec.get("failures", 0))
        alerted = int(rec.get("alerted", 0))
        res = results.get(name)

        if isinstance(res, list):
            if failures:
                if alerted:
                    log("source_recovered", source=name, failures=failures)
                    _admin(notifier, f"✅ TorinoAlert: la fonte {name} funziona di nuovo (dopo {failures} errori).")
                store.put(key, {"failures": 0, "alerted": 0, "updated_at": now})
            continue

        since = int(rec.get("since", now)) if failures else now
        failures += 1
        error = str(res)[:300]
        log("source_error", source=name, failures=failures, error=error)
        down_minutes = (now - since) / 60
        if failures >= 2 and down_minutes >= settings.health_alert_minutes and not alerted:
            alerted = 1
            log("source_down", source=name, failures=failures, error=error)
            _admin(
                notifier,
                f"⚠️ TorinoAlert: la fonte {name} non funziona da {down_minutes:.0f} minuti "
                f"({failures} tentativi).\nUltimo errore: {error}",
            )
        store.put(key, {"failures": failures, "since": since, "alerted": alerted, "last_error": error,
                        "updated_at": now})
