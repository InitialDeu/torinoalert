from datetime import datetime

from torinoalert import subscriptions
from torinoalert.config import Settings
from torinoalert.events import Event
from torinoalert.runner import bootstrap_key, health_key, is_silent, run
from torinoalert.sources import gtt
from torinoalert.store import MemoryStore
from torinoalert.telegram import TelegramError
from torinoalert.text import ROME

# Martedì 6 ottobre 2026, 12:00 a Roma: fuori dalla fascia notturna.
NOW = int(datetime(2026, 10, 6, 12, 0, tzinfo=ROME).timestamp())
NIGHT = int(datetime(2026, 10, 6, 2, 0, tzinfo=ROME).timestamp())
DAY = 86400


class FakeNotifier:
    def __init__(self, errors=None):
        self.sent: list[str] = []
        self.calls: list[dict] = []
        self.admin: list[str] = []
        self.errors = list(errors or [])  # eccezioni da sollevare ai prossimi send

    def send(self, text, chat_id=None, silent=False, reply_to=None):
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self.sent.append(text)
        self.calls.append({"text": text, "chat_id": chat_id, "silent": silent, "reply_to": reply_to})
        return 1000 + len(self.sent)

    def send_admin(self, text):
        self.admin.append(text)
        return True

    def channel(self):
        return [c for c in self.calls if c["chat_id"] is None]


def ev(id_, severity="MED", **kw):
    kw.setdefault("title", f"titolo {id_}")
    return Event(id=id_, source=kw.pop("source", "TEST"), severity=severity, **kw)


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

    # 5 giorni dopo: oltre metà TTL, rinnovo (conservando il message_id); mai rinviato
    r = do_run(src, store, tg, now=NOW + 5 * DAY)
    assert r["refreshed"] == 1 and store.data["a"]["expires_at"] == NOW + 12 * DAY
    assert store.data["a"]["msg"] == 1001
    assert len(tg.sent) == 1


def test_fingerprint_change_replies_to_original_message():
    store, tg = bootstrapped("S"), FakeNotifier()
    do_run({"S": lambda: [ev("r", fingerprint="v1")]}, store, tg)
    do_run({"S": lambda: [ev("r", fingerprint="v1")]}, store, tg)
    do_run({"S": lambda: [ev("r", fingerprint="v2")]}, store, tg)
    assert len(tg.sent) == 2
    assert "AGGIORNAMENTO" in tg.sent[1]
    assert tg.calls[1]["reply_to"] == 1001  # risponde al primo messaggio
    assert store.data["r"]["msg"] == 1002


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


def test_quiet_hours_and_low_severity_are_silent():
    s = settings()
    assert is_silent("LOW", NOW, s) and is_silent("INFO", NOW, s)
    assert not is_silent("MED", NOW, s) and not is_silent("HIGH", NOW, s)
    assert is_silent("HIGH", NIGHT, s)
    assert not is_silent("CRIT", NIGHT, s)

    store, tg = bootstrapped("S"), FakeNotifier()
    do_run({"S": lambda: [ev("a", "HIGH"), ev("b", "LOW")]}, store, tg)
    assert [c["silent"] for c in tg.calls] == [False, True]


def test_enrich_is_called_only_when_sending():
    calls = []

    def enrich():
        calls.append(1)
        return "corpo dell'articolo"

    store, tg = bootstrapped("S"), FakeNotifier()
    do_run({"S": lambda: [ev("a", enrich=enrich)]}, store, tg)
    do_run({"S": lambda: [ev("a", enrich=enrich)]}, store, tg)
    assert calls == [1]
    assert "corpo dell'articolo" in tg.sent[0]


def test_enrich_failure_still_sends():
    def boom():
        raise OSError("timeout")

    store, tg = bootstrapped("S"), FakeNotifier()
    do_run({"S": lambda: [ev("a", enrich=boom)]}, store, tg)
    assert len(tg.sent) == 1


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


def test_subscribers_get_gtt_events_in_private():
    store, tg = bootstrapped("GTT"), FakeNotifier()
    subscriptions.subscribe(store, 111, "13")
    subscriptions.subscribe(store, 222, "4")
    subscriptions.subscribe(store, 333, "METRO")

    events = [
        ev("g1", source=gtt.SOURCE, title="Linee 13 e 15 deviate in entrambe le direzioni."),
        ev("other", source="TRAFFICO", title="Corso linea 13 chiuso"),  # non GTT: niente privati
    ]
    r = do_run({"GTT": lambda: events}, store, tg)
    private = [c for c in tg.calls if c["chat_id"]]
    assert [c["chat_id"] for c in private] == ["111"]
    assert r["dm"] == 1 and len(tg.channel()) == 2


def test_dm_cap():
    store, tg = bootstrapped("GTT"), FakeNotifier()
    for chat in range(5):
        subscriptions.subscribe(store, chat, "4")
    r = do_run({"GTT": lambda: [ev("g", source=gtt.SOURCE, title="Linea 4 deviata")]}, store, tg, max_dm_per_run=3)
    assert r["dm"] == 3


def test_health_alert_after_minutes_and_recovery():
    store, tg = bootstrapped("S"), FakeNotifier()
    state = {"fail": True}

    def flaky():
        if state["fail"]:
            raise OSError("connection refused")
        return []

    for minute in (0, 10, 20):
        do_run({"S": flaky}, store, tg, now=NOW + minute * 60, health_alert_minutes=30)
    assert tg.admin == []  # 20 minuti: non ancora

    do_run({"S": flaky}, store, tg, now=NOW + 30 * 60, health_alert_minutes=30)
    assert len(tg.admin) == 1 and "da 30 minuti" in tg.admin[0]

    do_run({"S": flaky}, store, tg, now=NOW + 40 * 60, health_alert_minutes=30)
    assert len(tg.admin) == 1  # un solo avviso

    state["fail"] = False
    do_run({"S": flaky}, store, tg, now=NOW + 50 * 60, health_alert_minutes=30)
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
