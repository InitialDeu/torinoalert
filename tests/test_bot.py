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


def test_follow_topics():
    store = MemoryStore()
    assert "Ora segui «treni»" in reply(store, "/segui treni")
    assert "treni, aeroporto" in reply(store, "/segui aeroporto")
    assert "Argomenti che segui: treni, aeroporto" in reply(store, "/linee")
    assert "✅ treni" in reply(store, "/argomenti") and "▫️ traffico" in reply(store, "/argomenti")
    assert subscriptions.recipients(store, set(), {"treni"})[1] == {"treni": ["42"]}
    assert "Argomenti che segui: aeroporto" in reply(store, "/nonseguire treni")
    assert "nessuno" in reply(store, "/nonseguire")


def test_follow_invalid_topic_lists_topics():
    reply_text = reply(MemoryStore(), "/segui boh")
    assert reply_text.startswith("Argomento non valido") and "extraurbane" in reply_text
    assert reply(MemoryStore(), "/segui").startswith("Argomenti (/segui <nome>)")


def test_lines_and_topics_do_not_overwrite_each_other():
    store = MemoryStore()
    reply(store, "/linea 4")
    reply(store, "/segui treni")
    reply(store, "/stop 4")
    assert subscriptions.topics_of(store, 42) == ["treni"]


# ---------- riepilogo settimanale ----------

MONDAY = datetime(2026, 10, 12, 7, 5, tzinfo=ROME)


def planned(title, days=(), published=0):
    return Event(id=title, source="TRASPORTO PUBBLICO (GTT)", severity="LOW", title=title, topic="programmate",
                 planned=True, days=days, published=published)


def test_weekly_groups_by_day_and_skips_other_weeks():
    from torinoalert.digest import build_weekly

    events = [
        planned("Linea 9 deviata", ("2026-10-18",)),
        planned("Linea 17 deviata", ("2026-10-13", "2026-10-14")),
        planned("Linea 4 deviata", ("2026-11-02",)),                                   # fra tre settimane
        planned("Linea 38 deviata", (), published=int(MONDAY.timestamp()) - 86400),    # nuova, senza date
        planned("Linea 94 deviata", (), published=int(MONDAY.timestamp()) - 30 * 86400),  # vecchia
        Event(id="x", source="TRASPORTO PUBBLICO (GTT)", severity="MED", title="Linea 15 bloccata"),
    ]
    text = build_weekly(events, MONDAY)
    assert text.startswith("📅 GTT — deviazioni programmate da lun 12/10 a dom 18/10")
    assert text.index("▪️ mar 13/10") < text.index("Linea 17") < text.index("▪️ dom 18/10") < text.index("Linea 9")
    assert "Linea 38" in text and "fino a nuova comunicazione" in text
    assert "Linea 4 " not in text and "Linea 94" not in text and "Linea 15" not in text
    assert build_weekly([], MONDAY) is None


def test_weekly_sent_only_on_monday():
    store, tg = MemoryStore(), Notifier()
    sources = {"GTT": lambda: [planned("Linea 9 deviata", ("2026-10-13",))]}
    assert run_digest(sources, store, tg, Settings(), lambda: "", now=MONDAY)["weekly"] is True
    assert len(tg.sent) == 2 and tg.sent[1].startswith("📅 GTT")
    tuesday_store, tuesday_tg = MemoryStore(), Notifier()
    assert run_digest(sources, tuesday_store, tuesday_tg, Settings(), lambda: "",
                      now=MONDAY.replace(day=13))["weekly"] is False
