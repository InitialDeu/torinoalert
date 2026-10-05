"""
Un'esecuzione completa:
raccolta parallela -> dedup (con fingerprint e TTL rinnovato) -> bootstrap
silenzioso delle fonti nuove -> invio ordinato per gravità con tetto e gestione
429 -> salute delle fonti con avviso alla chat admin.
"""
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import Settings
from .events import SEVERITY_RANK, Event
from .log import log
from .telegram import TelegramError

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


def _record(ev: Event, now: int, ttl: int) -> dict:
    return {"fp": ev.fingerprint, "expires_at": now + ttl}


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

    pending: list[tuple[Event, bool]] = []  # (evento, è un aggiornamento)
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
                    pending.append((ev, False))
            elif ev.fingerprint and rec.get("fp") != ev.fingerprint:
                pending.append((ev, True))
            elif int(rec.get("expires_at", 0)) - now < ttl // 2:
                # Ancora pubblicato: rinnova il TTL, scadrà solo dopo che sparisce dalla fonte.
                store.put(ev.id, _record(ev, now, ttl))
                refreshed += 1
        if bootstrapping:
            store.put(bootstrap_key(name), {"created_at": now})
            log("source_bootstrapped", source=name, events=len(events))

    _update_health(sources, results, records, store, notifier, settings, now)

    pending.sort(key=lambda p: SEVERITY_RANK.get(p[0].severity, len(SEVERITY_RANK)))
    sent, dropped, failed = _send_all(pending, store, notifier, settings, now, ttl, deadline, sleep, clock)
    store.flush()

    summary = {
        "status": "ok",
        "sent": sent,
        "pending": len(pending) - sent - dropped,
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
    sent = dropped = failed = 0
    for ev, is_update in pending:
        if sent >= settings.max_sends_per_run:
            break
        if deadline is not None and clock() > deadline:
            log("send_deadline_reached")
            break
        if sent or failed:
            sleep(settings.send_interval)

        outcome = _deliver(notifier, ev.render(update=is_update), settings, deadline, sleep, clock)
        if outcome == "ok":
            store.put(ev.id, _record(ev, now, ttl))
            sent += 1
            log("sent", event_id=ev.id, severity=ev.severity, update=is_update)
        elif outcome == "drop":
            # Telegram rifiuta il messaggio (400): non riprovare all'infinito.
            store.put(ev.id, _record(ev, now, ttl))
            dropped += 1
        elif outcome == "stop":
            break
        else:
            failed += 1  # non marcato: si riprova alla prossima esecuzione
    return sent, dropped, failed


def _deliver(notifier, text, settings, deadline, sleep, clock) -> str:
    for attempt in range(2):
        try:
            notifier.send(text)
            return "ok"
        except TelegramError as e:
            if e.retry_after is None:
                log("send_failed", status=e.status, error=e.description)
                return "drop" if e.status == 400 else "fail"
            wait = float(e.retry_after)
            too_late = deadline is not None and clock() + wait > deadline
            if attempt or wait > settings.max_retry_wait or too_late:
                log("send_rate_limited", retry_after=wait)
                return "stop"
            sleep(wait)
        except Exception as e:
            log("send_failed", error=repr(e))
            return "fail"
    return "stop"


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

        failures += 1
        error = str(res)[:300]
        log("source_error", source=name, failures=failures, error=error)
        if failures >= settings.health_alert_after and not alerted:
            alerted = 1
            log("source_down", source=name, failures=failures, error=error)
            _admin(
                notifier,
                f"⚠️ TorinoAlert: la fonte {name} fallisce da {failures} esecuzioni consecutive.\n"
                f"Ultimo errore: {error}",
            )
        store.put(key, {"failures": failures, "alerted": alerted, "last_error": error, "updated_at": now})
