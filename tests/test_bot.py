from datetime import datetime

from torinoalert import bot, subscriptions
from torinoalert.config import Settings
from torinoalert.digest import build, run_digest
from torinoalert.events import Event
from torinoalert.store import MemoryStore
from torinoalert.text import ROME

MORNING = datetime(2026, 10, 6, 7, 5, tzinfo=ROME)


def private(text, chat_id=42):
    return {"message": {"chat": {"id": chat_id, "type": "private"}, "text": text}}


def reply(store, text, chat_id=42):
    return bot.handle_update(private(text, chat_id), store, lambda: "RIEPILOGO")["text"]


def test_subscribe_list_and_stop():
    store = MemoryStore()
    assert "Ora segui la linea 4" in reply(store, "/linea 4")
    assert "metro" in reply(store, "/linea metropolitana")
    assert "4, metro" in reply(store, "/linee")
    assert subscriptions.subscribers(store, {"4", "METRO"}) == {"4": ["42"], "METRO": ["42"]}

    assert "metro" in reply(store, "/stop 4")
    assert subscriptions.subscribers(store, {"4"}) == {"4": []}
    assert "nessuna" in reply(store, "/stop")


def test_invalid_line_and_unknown_command():
    store = MemoryStore()
    assert "Scrivi il numero" in reply(store, "/linea")
    assert "Scrivi il numero" in reply(store, "/linea ciao!")
    assert "non riconosciuto" in reply(store, "/boh")
    assert reply(store, "/oggi") == "RIEPILOGO"


def test_subscription_limit():
    store = MemoryStore()
    for n in range(subscriptions.MAX_LINES_PER_CHAT):
        reply(store, f"/linea {n + 1}")
    assert "al massimo" in reply(store, "/linea 999")


def test_groups_and_channels_ignored():
    store = MemoryStore()
    group = {"message": {"chat": {"id": -1, "type": "group"}, "text": "/linea 4"}}
    assert bot.handle_update(group, store, lambda: "") is None
    assert bot.handle_update({"channel_post": {}}, store, lambda: "") is None


def test_webhook_secret_is_stable_and_valid():
    s = bot.webhook_secret("123:abc")
    assert s == bot.webhook_secret("123:abc") != bot.webhook_secret("123:abd")
    assert len(s) == 48 and s.isalnum()


# ---------- RIEPILOGO ----------

def ev(source, line, id_=None):
    return Event(id=id_ or line, source=source, severity="MED", title=line, digest_line=line)


def test_digest_sections_ordered_and_capped():
    events = [ev("TRAFFICO", f"strada {i}") for i in range(9)] + [ev("SCIOPERI", "9/10 TPL")]
    text = build(events, "mar 6: nuvoloso 16°/24°", MORNING, failed=["SMAT"])
    assert text.startswith("☀️ TORINO — martedì 6 ottobre")
    assert text.index("SCIOPERI") < text.index("TRAFFICO")
    assert "… e altri 3" in text and "Meteo" in text and "SMAT" in text


def test_digest_says_when_all_clear():
    text = build([ev("SEMAFORO ANTISMOG", "livello 0")], "", MORNING)
    assert "Nessun disservizio" in text


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, text, **kw):
        self.sent.append(text)
        return 1


def test_digest_once_per_day_at_configured_hour():
    store, tg = MemoryStore(), Notifier()
    sources = {"X": lambda: [ev("TRAFFICO", "corso Francia chiuso")]}
    s = Settings()

    assert run_digest(sources, store, tg, s, lambda: "", now=MORNING.replace(hour=6))["status"] == "skipped"
    assert run_digest(sources, store, tg, s, lambda: "", now=MORNING)["status"] == "sent"
    assert run_digest(sources, store, tg, s, lambda: "", now=MORNING)["status"] == "skipped"
    assert len(tg.sent) == 1 and "corso Francia" in tg.sent[0]


def test_digest_survives_weather_failure():
    def boom():
        raise OSError("down")

    store, tg = MemoryStore(), Notifier()
    run_digest({}, store, tg, Settings(), boom, now=MORNING)
    assert len(tg.sent) == 1


def test_id_command():
    assert reply(MemoryStore(), "/id", chat_id=987654) == "Il tuo chat ID è: 987654"
