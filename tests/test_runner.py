from torinoalert.config import Settings
from torinoalert.events import Event
from torinoalert.runner import bootstrap_key, health_key, run
from torinoalert.store import MemoryStore
from torinoalert.telegram import TelegramError

NOW = 1_800_000_000
DAY = 86400


class FakeNotifier:
    def __init__(self, errors=None):
        self.sent: list[str] = []
        self.admin: list[str] = []
        self.errors = list(errors or [])  # eccezioni da sollevare ai prossimi send

    def send(self, text, chat_id=None):
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self.sent.append(text)

    def send_admin(self, text):
        self.admin.append(text)
        return True


def ev(id_, severity="MED", **kw):
    return Event(id=id_, source="TEST", severity=severity, title=f"titolo {id_}", **kw)


def settings(**kw):
    return Settings(send_interval=0, **kw)


def bootstrapped(*sources):
    return MemoryStore({bootstrap_key(s): {"created_at": 0} for s in sources})


def do_run(sources, store, notifier, now=NOW, **kw):
    return run(sources, store, notifier, settings(**kw), now=now, sleep=lambda s: None)


def test_first_run_is_silent_then_only_new_events_are_sent():
    store, tg = MemoryStore(), FakeNotifier()

    r = do_run({"S": lambda: [ev("a"), ev("b")]}, store, tg)
    assert tg.sent == [] and r["silenced"] == 2

    r = do_run({"S": lambda: [ev("a"), ev("b"), ev("c")]}, store, tg)
    assert len(tg.sent) == 1 and "titolo c" in tg.sent[0]


def test_new_source_bootstraps_independently():
    store, tg = bootstrapped("OLD"), FakeNotifier()
    do_run({"OLD": lambda: [ev("x")], "NEW": lambda: [ev("y")]}, store, tg)
    assert len(tg.sent) == 1 and "titolo x" in tg.sent[0]


def test_failed_source_does_not_bootstrap():
    store, tg = MemoryStore(), FakeNotifier()

    def boom():
        raise OSError("down")

    do_run({"S": boom}, store, tg)
    assert bootstrap_key("S") not in store.data


def test_dedup_and_ttl_refresh_while_still_published():
    store, tg = bootstrapped("S"), FakeNotifier()
    src = {"S": lambda: [ev("a")]}

    do_run(src, store, tg, now=NOW)
    assert len(tg.sent) == 1
    first_expiry = store.data["a"]["expires_at"]

    # 2 giorni dopo: ancora fresco, nessuna scrittura
    r = do_run(src, store, tg, now=NOW + 2 * DAY)
    assert r["refreshed"] == 0 and store.data["a"]["expires_at"] == first_expiry

    # 5 giorni dopo: oltre metà TTL, rinnovo; mai rinviato
    r = do_run(src, store, tg, now=NOW + 5 * DAY)
    assert r["refreshed"] == 1 and store.data["a"]["expires_at"] == NOW + 12 * DAY
    assert len(tg.sent) == 1


def test_fingerprint_change_sends_update():
    store, tg = bootstrapped("S"), FakeNotifier()
    do_run({"S": lambda: [ev("r", fingerprint="v1")]}, store, tg)
    do_run({"S": lambda: [ev("r", fingerprint="v1")]}, store, tg)
    do_run({"S": lambda: [ev("r", fingerprint="v2")]}, store, tg)
    assert len(tg.sent) == 2
    assert "AGGIORNAMENTO" in tg.sent[1]


def test_silent_if_new_then_change_is_notified():
    store, tg = bootstrapped("S"), FakeNotifier()
    do_run({"S": lambda: [ev("arpa", fingerprint="VERDE", silent_if_new=True)]}, store, tg)
    assert tg.sent == []
    do_run({"S": lambda: [ev("arpa", severity="HIGH", fingerprint="TEMPORALI=2")]}, store, tg)
    do_run({"S": lambda: [ev("arpa", fingerprint="VERDE", silent_if_new=True)]}, store, tg)
    assert len(tg.sent) == 2  # allerta + rientro


def test_cap_per_run_and_severity_order():
    store, tg = bootstrapped("S"), FakeNotifier()
    events = [ev("info", "INFO"), ev("med", "MED"), ev("crit", "CRIT"), ev("high", "HIGH")]
    r = do_run({"S": lambda: events}, store, tg, max_sends_per_run=2)
    assert ["titolo crit" in tg.sent[0], "titolo high" in tg.sent[1]] == [True, True]
    assert r["pending"] == 2

    do_run({"S": lambda: events}, store, tg, max_sends_per_run=2)
    assert len(tg.sent) == 4


def test_rate_limit_short_wait_retries():
    store, tg = bootstrapped("S"), FakeNotifier(errors=[TelegramError(429, "Too Many", retry_after=3)])
    r = do_run({"S": lambda: [ev("a")]}, store, tg)
    assert r["sent"] == 1


def test_rate_limit_long_wait_stops_and_retries_next_run():
    store, tg = bootstrapped("S"), FakeNotifier(errors=[TelegramError(429, "Too Many", retry_after=60)])
    src = {"S": lambda: [ev("a"), ev("b")]}
    r = do_run(src, store, tg)
    assert r["sent"] == 0 and "a" not in store.data

    do_run(src, store, tg)
    assert len(tg.sent) == 2


def test_bad_request_is_dropped_not_retried_forever():
    store, tg = bootstrapped("S"), FakeNotifier(errors=[TelegramError(400, "Bad Request")])
    r = do_run({"S": lambda: [ev("a"), ev("b")]}, store, tg)
    assert r["dropped"] == 1 and r["sent"] == 1
    do_run({"S": lambda: [ev("a"), ev("b")]}, store, tg)
    assert len(tg.sent) == 1


def test_transient_error_is_retried_next_run():
    store, tg = bootstrapped("S"), FakeNotifier(errors=[OSError("timeout")])
    do_run({"S": lambda: [ev("a")]}, store, tg)
    assert tg.sent == []
    do_run({"S": lambda: [ev("a")]}, store, tg)
    assert len(tg.sent) == 1


def test_duplicate_ids_across_sources_sent_once():
    store, tg = bootstrapped("A", "B"), FakeNotifier()
    do_run({"A": lambda: [ev("same")], "B": lambda: [ev("same")]}, store, tg)
    assert len(tg.sent) == 1


def test_health_alert_after_threshold_and_recovery():
    store, tg = bootstrapped("S"), FakeNotifier()
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise OSError("connection refused")
        return []

    for _ in range(3):
        do_run({"S": flaky}, store, tg, health_alert_after=3)
    assert len(tg.admin) == 1 and "fallisce da 3" in tg.admin[0]

    do_run({"S": flaky}, store, tg, health_alert_after=3)
    assert len(tg.admin) == 1  # un solo avviso

    state["fail"] = False
    do_run({"S": flaky}, store, tg, health_alert_after=3)
    assert len(tg.admin) == 2 and "funziona di nuovo" in tg.admin[1]
    assert store.data[health_key("S")]["failures"] == 0


def test_one_slow_source_does_not_block_others():
    import time

    store, tg = bootstrapped("FAST", "SLOW"), FakeNotifier()
    r = run(
        {"FAST": lambda: [ev("a")], "SLOW": lambda: time.sleep(2) or []},
        store, tg, Settings(send_interval=0, collect_timeout=0.5), now=NOW,
    )
    assert r["sent"] == 1
    assert "error" in r["per_source"]["SLOW"]
