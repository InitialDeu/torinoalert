"""
Iscrizioni, nella stessa tabella dello stato:
  sub:chat:<chat_id>   -> {"lines": "4,13,METRO", "topics": "treni,aeroporto"}
  sub:line:<LINEA>     -> {"chats": "111,222"}
  sub:topic:<ARGOMENTO> -> {"chats": "111,222"}
"""
MAX_LINES_PER_CHAT = 20

# Argomenti che non vanno sul canale ma solo a chi li segue con /segui.
TOPICS = {
    "programmate": "deviazioni GTT urbane programmate, una per una (sul canale c'è il riepilogo del lunedì)",
    "extraurbane": "linee GTT extraurbane e trasporto pubblico regionale",
    "fermate": "singole fermate GTT sospese o spostate",
    "treni": "lavori e disservizi minori sui treni (le sospensioni gravi sono già sul canale)",
    "traffico": "autostrade, tangenziale, statali e programmi chiusure A32/Frejus",
    "provinciali": "strade provinciali della Città metropolitana",
    "aeroporto": "ritardi dei voli a Caselle (le cancellazioni sono già sul canale)",
}


def chat_key(chat_id) -> str:
    return f"sub:chat:{chat_id}"


def line_key(line: str) -> str:
    return f"sub:line:{line}"


def topic_key(topic: str) -> str:
    return f"sub:topic:{topic}"


def _split(value: str) -> list[str]:
    return [x for x in (value or "").split(",") if x]


def _chat(store, chat_id) -> dict[str, list[str]]:
    rec = store.get_many([chat_key(chat_id)]).get(chat_key(chat_id), {})
    return {"lines": _split(rec.get("lines", "")), "topics": _split(rec.get("topics", ""))}


def _save_chat(store, chat_id, chat: dict[str, list[str]]) -> None:
    store.put(chat_key(chat_id), {"lines": ",".join(chat["lines"]), "topics": ",".join(chat["topics"])})


def _set_member(store, key: str, chat_id, present: bool) -> None:
    chats = _split(store.get_many([key]).get(key, {}).get("chats", ""))
    cid = str(chat_id)
    if present and cid not in chats:
        chats.append(cid)
    elif not present and cid in chats:
        chats.remove(cid)
    store.put(key, {"chats": ",".join(chats)})


# ---------- linee ----------

def lines_of(store, chat_id) -> list[str]:
    return _chat(store, chat_id)["lines"]


def subscribe(store, chat_id, line: str) -> list[str]:
    chat = _chat(store, chat_id)
    if line in chat["lines"]:
        return chat["lines"]
    if len(chat["lines"]) >= MAX_LINES_PER_CHAT:
        raise ValueError(f"puoi seguire al massimo {MAX_LINES_PER_CHAT} linee")
    chat["lines"].append(line)
    _save_chat(store, chat_id, chat)
    _set_member(store, line_key(line), chat_id, True)
    return chat["lines"]


def unsubscribe(store, chat_id, line: str | None = None) -> list[str]:
    chat = _chat(store, chat_id)
    removed = [x for x in chat["lines"] if line is None or x == line]
    for x in removed:
        _set_member(store, line_key(x), chat_id, False)
    chat["lines"] = [x for x in chat["lines"] if x not in removed]
    _save_chat(store, chat_id, chat)
    return chat["lines"]


# ---------- argomenti ----------

def topics_of(store, chat_id) -> list[str]:
    return _chat(store, chat_id)["topics"]


def follow(store, chat_id, topic: str) -> list[str]:
    if topic not in TOPICS:
        raise ValueError(f"argomento sconosciuto: {topic}")
    chat = _chat(store, chat_id)
    if topic not in chat["topics"]:
        chat["topics"].append(topic)
        _save_chat(store, chat_id, chat)
        _set_member(store, topic_key(topic), chat_id, True)
    return chat["topics"]


def unfollow(store, chat_id, topic: str | None = None) -> list[str]:
    chat = _chat(store, chat_id)
    removed = [x for x in chat["topics"] if topic is None or x == topic]
    for x in removed:
        _set_member(store, topic_key(x), chat_id, False)
    chat["topics"] = [x for x in chat["topics"] if x not in removed]
    _save_chat(store, chat_id, chat)
    return chat["topics"]


# ---------- destinatari ----------

def subscribers(store, lines: set[str]) -> dict[str, list[str]]:
    """Linea -> chat iscritte, con una sola lettura batch."""
    return recipients(store, lines, set())[0]


def recipients(store, lines: set[str], topics: set[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(linea -> chat, argomento -> chat) con una sola lettura batch."""
    keys = [line_key(x) for x in sorted(lines)] + [topic_key(x) for x in sorted(topics)]
    records = store.get_many(keys) if keys else {}
    by_line = {x: _split(records.get(line_key(x), {}).get("chats", "")) for x in sorted(lines)}
    by_topic = {x: _split(records.get(topic_key(x), {}).get("chats", "")) for x in sorted(topics)}
    return by_line, by_topic
