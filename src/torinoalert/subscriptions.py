"""
Iscrizioni alle linee GTT, nella stessa tabella dello stato:
  sub:chat:<chat_id> -> {"lines": "4,13,METRO"}
  sub:line:<LINEA>   -> {"chats": "111,222"}
"""
MAX_LINES_PER_CHAT = 20


def chat_key(chat_id) -> str:
    return f"sub:chat:{chat_id}"


def line_key(line: str) -> str:
    return f"sub:line:{line}"


def _split(value: str) -> list[str]:
    return [x for x in (value or "").split(",") if x]


def lines_of(store, chat_id) -> list[str]:
    return _split(store.get_many([chat_key(chat_id)]).get(chat_key(chat_id), {}).get("lines", ""))


def _set_chat_on_line(store, line: str, chat_id, present: bool) -> None:
    key = line_key(line)
    chats = _split(store.get_many([key]).get(key, {}).get("chats", ""))
    cid = str(chat_id)
    if present and cid not in chats:
        chats.append(cid)
    elif not present and cid in chats:
        chats.remove(cid)
    store.put(key, {"chats": ",".join(chats)})


def subscribe(store, chat_id, line: str) -> list[str]:
    lines = lines_of(store, chat_id)
    if line in lines:
        return lines
    if len(lines) >= MAX_LINES_PER_CHAT:
        raise ValueError(f"puoi seguire al massimo {MAX_LINES_PER_CHAT} linee")
    lines.append(line)
    store.put(chat_key(chat_id), {"lines": ",".join(lines)})
    _set_chat_on_line(store, line, chat_id, True)
    return lines


def unsubscribe(store, chat_id, line: str | None = None) -> list[str]:
    lines = lines_of(store, chat_id)
    removed = [x for x in lines if line is None or x == line]
    for x in removed:
        _set_chat_on_line(store, x, chat_id, False)
    remaining = [x for x in lines if x not in removed]
    store.put(chat_key(chat_id), {"lines": ",".join(remaining)})
    return remaining


def subscribers(store, lines: set[str]) -> dict[str, list[str]]:
    """Linea -> chat iscritte, con una sola lettura batch."""
    if not lines:
        return {}
    records = store.get_many([line_key(x) for x in sorted(lines)])
    return {x: _split(records.get(line_key(x), {}).get("chats", "")) for x in sorted(lines)}
