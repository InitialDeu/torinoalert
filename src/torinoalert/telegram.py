import json
import urllib.error
import urllib.request

COMMANDS = [
    ("oggi", "Riepilogo dei disservizi attivi adesso"),
    ("linea", "Segui una linea GTT, es. /linea 4 o /linea metro"),
    ("stop", "Smetti di seguire una linea, es. /stop 4 (senza numero: tutte)"),
    ("segui", "Segui un argomento: treni, traffico, aeroporto, ..."),
    ("nonseguire", "Smetti di seguire un argomento"),
    ("argomenti", "Elenco degli argomenti"),
    ("linee", "Linee e argomenti che segui"),
    ("id", "Il tuo chat ID"),
    ("help", "Come funziona il bot"),
]


class TelegramError(Exception):
    def __init__(self, status: int | None, description: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


class Telegram:
    """Invio testo semplice (niente parse_mode: nessun 400 da markdown malformato)."""

    def __init__(self, token: str, chat_id: str, admin_chat_id: str = "", timeout: float = 15):
        self.token = token
        self.chat_id = chat_id
        self.admin_chat_id = admin_chat_id
        self.timeout = timeout

    def call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8")).get("result") or {}
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", errors="ignore"))
            except ValueError:
                body = {}
            raise TelegramError(
                e.code,
                str(body.get("description") or e.reason)[:300],
                (body.get("parameters") or {}).get("retry_after"),
            ) from None

    def send(self, text: str, chat_id: str | None = None, silent: bool = False, reply_to: int | None = None) -> int:
        """Invia e restituisce il message_id (serve per rispondere agli aggiornamenti)."""
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
            "disable_notification": silent,
        }
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        return int(self.call("sendMessage", payload).get("message_id") or 0)

    def send_admin(self, text: str) -> bool:
        if not self.admin_chat_id:
            return False
        self.send(text, chat_id=self.admin_chat_id)
        return True

    def pin(self, message_id: int, chat_id: str | None = None) -> None:
        """Fissa un messaggio (nel canale serve il permesso di modificare i messaggi)."""
        self.call("pinChatMessage", {
            "chat_id": chat_id or self.chat_id, "message_id": int(message_id), "disable_notification": True,
        })

    def unpin(self, message_id: int, chat_id: str | None = None) -> None:
        self.call("unpinChatMessage", {"chat_id": chat_id or self.chat_id, "message_id": int(message_id)})

    def set_webhook(self, url: str, secret: str) -> None:
        self.call("setWebhook", {
            "url": url,
            "secret_token": secret,
            "allowed_updates": ["message"],
            "drop_pending_updates": True,
        })
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})


class DryRunNotifier:
    """Stampa i messaggi invece di inviarli (runner locale --dry-run)."""

    def __init__(self):
        self._next_id = 0

    def send(self, text: str, chat_id: str | None = None, silent: bool = False, reply_to: int | None = None) -> int:
        self._next_id += 1
        flags = " ".join(f for f, on in (("[silenzioso]", silent), (f"[risposta a {reply_to}]", reply_to)) if on)
        print("-" * 60 + (f" → {chat_id}" if chat_id else "") + (f" {flags}" if flags else ""))
        print(text)
        return self._next_id

    def send_admin(self, text: str) -> bool:
        print(f"[ADMIN] {text}")
        return True

    def pin(self, message_id: int, chat_id: str | None = None) -> None:
        print(f"[fissato in alto il messaggio {message_id}]")

    def unpin(self, message_id: int, chat_id: str | None = None) -> None:
        print(f"[tolto dai fissati il messaggio {message_id}]")
