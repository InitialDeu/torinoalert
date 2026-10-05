"""Entry point AWS Lambda (handler = "handler.lambda_handler")."""
import os
import time

import boto3

from torinoalert.config import Settings
from torinoalert.log import log
from torinoalert.runner import run
from torinoalert.sources import default_sources
from torinoalert.store import DynamoStore
from torinoalert.telegram import Telegram

_ssm = boto3.client("ssm")
_telegram: Telegram | None = None
_store: DynamoStore | None = None


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


def lambda_handler(event, context):
    global _store
    settings = Settings.from_env()
    if _store is None:
        _store = DynamoStore(os.environ["DDB_TABLE"])

    # Margine di 5s prima del timeout Lambda per chiudere le scritture.
    deadline = None
    if context is not None:
        deadline = time.monotonic() + context.get_remaining_time_in_millis() / 1000 - 5

    try:
        return run(default_sources(settings), _store, _get_telegram(), settings, deadline=deadline)
    except Exception as e:
        # Rilancia: l'errore finisce nella metrica Errors e fa scattare l'allarme CloudWatch.
        log("fatal_error", error=repr(e))
        raise
