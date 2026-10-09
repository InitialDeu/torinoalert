"""Comandi del bot in chat privata (webhook Telegram)."""
import hashlib
import hmac
from collections.abc import Callable

from . import subscriptions
from .text import normalize_line

HELP = (
    "🚦 TorinoAlert — avvisi su trasporti, traffico, meteo e servizi a Torino.\n\n"
    "Il canale riceve ciò che riguarda tutta la città. Qui, in privato, puoi aggiungere "
    "le tue linee GTT e gli argomenti di dettaglio:\n\n"
    "/linea 4 — segui la linea 4 (anche /linea metro, /linea SE2, /linea 1432)\n"
    "/stop 4 — smetti di seguirla (/stop da solo: tutte)\n"
    "/segui treni — segui un argomento (/argomenti per l'elenco)\n"
    "/nonseguire treni — smetti di seguirlo (da solo: tutti)\n"
    "/linee — linee e argomenti che segui\n"
    "/oggi — riepilogo dei disservizi attivi adesso\n"
    "/id — il tuo chat ID (per configurare gli avvisi tecnici)\n"
    "/help — questo messaggio"
)


def webhook_secret(token: str) -> str:
    """Secret del webhook derivato dal token del bot: niente da salvare a parte."""
    return hmac.new(token.encode(), b"torinoalert-webhook", hashlib.sha256).hexdigest()[:48]


def _fmt(lines: list[str]) -> str:
    return ", ".join("metro" if x == "METRO" else x for x in lines) if lines else "nessuna"


def _fmt_topics(topics: list[str]) -> str:
    return ", ".join(topics) if topics else "nessuno"


def topics_text(followed: list[str]) -> str:
    rows = [f"{'✅' if t in followed else '▫️'} {t} — {desc}" for t, desc in subscriptions.TOPICS.items()]
    return "Argomenti (/segui <nome>):\n\n" + "\n".join(rows)


def reply_for(text: str, chat_id, store, today: Callable[[], str]) -> str:
    cmd, _, arg = (text or "").strip().partition(" ")
    cmd = cmd.lstrip("/").split("@")[0].lower()
    arg = arg.strip()

    if cmd in ("start", "help", "aiuto"):
        return HELP
    if cmd == "id":
        return f"Il tuo chat ID è: {chat_id}"
    if cmd == "oggi":
        return today()
    if cmd == "linee":
        lines = subscriptions.lines_of(store, chat_id)
        topics = subscriptions.topics_of(store, chat_id)
        return f"Linee che segui: {_fmt(lines)}\nArgomenti che segui: {_fmt_topics(topics)}"
    if cmd == "argomenti":
        return topics_text(subscriptions.topics_of(store, chat_id))
    if cmd == "segui":
        topic = arg.lower()
        if topic not in subscriptions.TOPICS:
            prefix = "Argomento non valido.\n\n" if topic else ""
            return prefix + topics_text(subscriptions.topics_of(store, chat_id))
        topics = subscriptions.follow(store, chat_id, topic)
        return f"✅ Ora segui «{topic}».\nArgomenti che segui: {_fmt_topics(topics)}"
    if cmd == "nonseguire":
        topic = arg.lower() or None
        if topic and topic not in subscriptions.TOPICS:
            return "Argomento non valido.\n\n" + topics_text(subscriptions.topics_of(store, chat_id))
        topics = subscriptions.unfollow(store, chat_id, topic)
        return f"🛑 Fatto. Argomenti che segui: {_fmt_topics(topics)}"
    if cmd == "linea":
        line = normalize_line(arg)
        if not line:
            return "Scrivi il numero della linea, es. /linea 4, /linea 15, /linea SE2 o /linea metro."
        try:
            lines = subscriptions.subscribe(store, chat_id, line)
        except ValueError as e:
            return f"⚠️ Non posso aggiungerla: {e}."
        return f"✅ Ora segui la linea {_fmt([line])}.\nLinee che segui: {_fmt(lines)}"
    if cmd == "stop":
        line = normalize_line(arg) if arg else None
        if arg and not line:
            return "Linea non valida. Usa /stop 4, oppure /stop da solo per smettere di seguirle tutte."
        lines = subscriptions.unsubscribe(store, chat_id, line)
        return f"🛑 Fatto. Linee che segui: {_fmt(lines)}"
    return "Comando non riconosciuto.\n\n" + HELP


def handle_update(update: dict, store, today: Callable[[], str]) -> dict | None:
    """Risposta da restituire nel corpo del webhook (Telegram la esegue come sendMessage)."""
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private" or not msg.get("text"):
        return None  # gruppi, canali, foto: ignorati
    return {
        "method": "sendMessage",
        "chat_id": chat["id"],
        "text": reply_for(msg["text"], chat["id"], store, today),
        "link_preview_options": {"is_disabled": True},
    }
