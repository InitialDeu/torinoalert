"""
Entry point AWS Lambda:
  handler.lambda_handler  -> schedule ogni N minuti ({"mode": "digest"} per il riepilogo del mattino)
  handler.webhook_handler -> Function URL chiamata da Telegram per i comandi in chat privata
"""
import base64
import dataclasses
import hmac
import json
import os
import time

import boto3

from torinoalert import bot
from torinoalert.config import Settings
from torinoalert.digest import collect_digest, run_digest
from torinoalert.log import log
from torinoalert.runner import run
from torinoalert.sources import all_sources, default_sources, due_sources, weather_line
from torinoalert.store import DynamoStore
from torinoalert.telegram import COMMANDS, Telegram
from torinoalert.text import sha

_ssm = boto3.client("ssm")
_telegram: Telegram | None = None
_store: DynamoStore | None = None

WEBHOOK_META = "__meta__:webhook"


def _get_telegram() -> Telegram:
    """Secret letti da SSM una volta per container (meno chiamate KMS)."""
    global _telegram
    if _telegram is None:
        names = [os.environ["SSM_TOKEN_PARAM"], os.environ["SSM_CHATID_PARAM"]]
        resp = _ssm.get_parameters(Names=names, WithDecryption=True)
        values = {p["Name"]: p["Value"] for p in resp["Parameters"]}
        missing = [n for n in names if not values.get(n)]
        if missing:
            raise RuntimeError(f"Parametri SSM mancanti: {missing}")
        _telegram = Telegram(values[names[0]], values[names[1]], admin_chat_id=os.environ.get("ADMIN_CHAT_ID", ""))
    return _telegram


def _get_store() -> DynamoStore:
    global _store
    if _store is None:
        _store = DynamoStore(os.environ["DDB_TABLE"])
    return _store


def _ensure_webhook(telegram: Telegram, store: DynamoStore) -> None:
    """Registra webhook e menu comandi su Telegram quando cambiano l'URL o i comandi."""
    url = os.environ.get("WEBHOOK_URL", "")
    if not url:
        return
    commands = sha(repr(COMMANDS))[:12]
    current = store.get_many([WEBHOOK_META]).get(WEBHOOK_META, {})
    if current.get("url") == url and current.get("commands") == commands:
        return
    try:
        telegram.set_webhook(url, bot.webhook_secret(telegram.token))
        store.put(WEBHOOK_META, {"url": url, "commands": commands, "updated_at": int(time.time())})
        log("webhook_registered", url=url)
    except Exception as e:
        log("webhook_register_failed", error=repr(e))


def lambda_handler(event, context):
    settings = Settings.from_env()
    store = _get_store()

    # Margine di 5s prima del timeout Lambda per chiudere le scritture.
    deadline = None
    if context is not None:
        deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000 - 5

    try:
        telegram = _get_telegram()
        if (event or {}).get("mode") == "digest":
            return run_digest(default_sources(settings), store, telegram, settings, lambda: weather_line(settings))
        _ensure_webhook(telegram, store)
        sources = due_sources(all_sources(settings), time.time(), settings.schedule_rate_minutes)
        return run(sources, store, telegram, settings, deadline=deadline)
    except Exception as e:
        # Rilancia: l'errore finisce nella metrica Errors e fa scattare l'allarme CloudWatch.
        log("fatal_error", error=repr(e))
        raise


def _response(status: int, payload: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload or {"ok": status == 200}, ensure_ascii=False),
    }


def webhook_handler(event, context):
    telegram = _get_telegram()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not hmac.compare_digest(headers.get("x-telegram-bot-api-secret-token", ""), bot.webhook_secret(telegram.token)):
        return _response(401)

    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        update = json.loads(body)
    except ValueError:
        return _response(200)  # non ritentare aggiornamenti malformati

    settings = dataclasses.replace(Settings.from_env(), collect_timeout=15)

    def today() -> str:
        return collect_digest(default_sources(settings), settings, lambda: weather_line(settings))

    try:
        reply = bot.handle_update(update, _get_store(), today)
    except Exception as e:
        log("webhook_error", error=repr(e))
        return _response(200)  # Telegram ritenterebbe all'infinito
    if reply:
        log("command", text=((update.get("message") or {}).get("text") or "")[:40])
    return _response(200, reply)
