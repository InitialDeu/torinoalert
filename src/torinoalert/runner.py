"""
Un'esecuzione completa:
raccolta parallela -> dedup (con fingerprint e TTL rinnovato) -> bootstrap
silenzioso delle fonti nuove -> invio al canale ordinato per gravità (con
tetto, 429, notte silenziosa, risposte al messaggio originale) -> messaggi
privati agli iscritti alle linee -> salute delle fonti con avviso admin.
"""
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import subscriptions
from .config import Settings
from .events import SEVERITY_RANK, Event
from .log import log
from .sources import gtt
from .telegram import TelegramError
from .text import ROME, lines_in_text

Sources = dict[str, Callable[[], list[Event]]]


def bootstrap_key(source: str) -> str:
    return f"__meta__:bootstrap:{source}"


def health_key(source: str) -> str:
    return f"__meta__:health:{source}"


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


def _record(ev: Event, now: int, ttl: int, msg: int | None = None) -> dict:
    item = {"fp": ev.fingerprint, "expires_at": now + ttl}
    if msg:
        item["msg"] = int(msg)
    return item


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
    )

    pending: list[tuple[Event, dict | None]] = []  # (evento, record esistente se è un aggiornamento)
    silenced = refreshed = 0
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
                pending.append((ev, rec))
            elif int(rec.get("expires_at", 0)) - now < ttl // 2:
                # Ancora pubblicato: rinnova il TTL, scadrà solo dopo che sparisce dalla fonte.
                store.put(ev.id, _record(ev, now, ttl, rec.get("msg")))
                refreshed += 1
        if bootstrapping:
            store.put(bootstrap_key(name), {"created_at": now})
            log("source_bootstrapped", source=name, events=len(events))

    _update_health(sources, results, records, store, notifier, settings, now)

    pending.sort(key=lambda p: SEVERITY_RANK.get(p[0].severity, len(SEVERITY_RANK)))
    sent_events, dropped, failed = _send_all(pending, store, notifier, settings, now, ttl, deadline, sleep, clock)
    dms = _notify_subscribers(sent_events, store, notifier, settings, now, deadline, clock)
    store.flush()

    summary = {
        "status": "ok",
        "sent": len(sent_events),
        "dm": dms,
        "pending": len(pending) - len(sent_events) - dropped,
        "dropped": dropped,
        "failed": failed,
        "silenced": silenced,
        "refreshed": refreshed,
        "events": len(ids),
        "per_source": {
            n: (len(r) if isinstance(r, list) else f"error: {r}") for n, r in results.items()
        },
        "duration": round(clock() - started, 2),
    }
    log("run_summary", **summary)
    return summary


def _send_all(pending, store, notifier, settings, now, ttl, deadline, sleep, clock):
    sent: list[tuple[Event, str]] = []
    dropped = failed = 0
    for ev, rec in pending:
        if len(sent) >= settings.max_sends_per_run:
            break
        if deadline is not None and clock() > deadline:
            log("send_deadline_reached")
            break
        if sent or failed:
            sleep(settings.send_interval)

        is_update = rec is not None
        extra = ""
        if ev.enrich and not is_update:
            try:
                extra = ev.enrich()
            except Exception as e:
                log("enrich_failed", event_id=ev.id, error=repr(e))
        text = ev.render(update=is_update, extra=extra)
        kwargs = {"silent": is_silent(ev.severity, now, settings)}
        if is_update and rec.get("msg"):
            kwargs["reply_to"] = int(rec["msg"])

        outcome, msg_id = _deliver(notifier, text, kwargs, settings, deadline, sleep, clock)
        if outcome == "ok":
            store.put(ev.id, _record(ev, now, ttl, msg_id or (rec or {}).get("msg")))
            sent.append((ev, text))
            log("sent", event_id=ev.id, severity=ev.severity, update=is_update, silent=kwargs["silent"])
        elif outcome == "drop":
            # Telegram rifiuta il messaggio (400): non riprovare all'infinito.
            store.put(ev.id, _record(ev, now, ttl))
            dropped += 1
        elif outcome == "stop":
            break
        else:
            failed += 1  # non marcato: si riprova alla prossima esecuzione
    return sent, dropped, failed


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


def _notify_subscribers(sent, store, notifier, settings, now, deadline, clock) -> int:
    """Inoltra in privato gli avvisi GTT a chi segue le linee citate."""
    by_event = [(ev, text, lines_in_text(f"{ev.title} {ev.body}")) for ev, text in sent if ev.source == gtt.SOURCE]
    wanted = set().union(*(lines for _, _, lines in by_event)) if by_event else set()
    if not wanted:
        return 0
    subs = subscriptions.subscribers(store, wanted)

    count = 0
    for ev, text, lines in by_event:
        chats = {c for line in lines for c in subs.get(line, [])}
        for chat in sorted(chats):
            if count >= settings.max_dm_per_run or (deadline is not None and clock() > deadline):
                log("dm_cap_reached", sent=count)
                return count
            try:
                notifier.send(text, chat_id=chat, silent=is_silent(ev.severity, now, settings))
                count += 1
            except Exception as e:
                # 403: l'utente ha bloccato il bot; si continua con gli altri.
                log("dm_failed", chat=chat, error=repr(e))
    return count


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
