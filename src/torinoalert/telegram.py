import json
import urllib.error
import urllib.request


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

    def send(self, text: str, chat_id: str | None = None) -> None:
        payload = json.dumps({
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
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

    def send_admin(self, text: str) -> bool:
        if not self.admin_chat_id:
            return False
        self.send(text, chat_id=self.admin_chat_id)
        return True


class DryRunNotifier:
    """Stampa i messaggi invece di inviarli (runner locale --dry-run)."""

    def send(self, text: str, chat_id: str | None = None) -> None:
        print("-" * 60)
        print(text)

    def send_admin(self, text: str) -> bool:
        print(f"[ADMIN] {text}")
        return True
