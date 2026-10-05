"""
Runner locale: stessa logica della Lambda, stato su file JSON invece di DynamoDB.

  python run_local.py --once --dry-run --no-bootstrap   # mostra cosa verrebbe inviato
  python run_local.py                                   # loop ogni 120s, invia davvero
"""
import argparse
import dataclasses
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from torinoalert.config import Settings  # noqa: E402
from torinoalert.runner import run  # noqa: E402
from torinoalert.sources import default_sources  # noqa: E402
from torinoalert.store import FileStore, MemoryStore  # noqa: E402
from torinoalert.telegram import DryRunNotifier, Telegram  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="una sola esecuzione")
    parser.add_argument("--interval", type=int, default=120, help="secondi tra le esecuzioni (default 120)")
    parser.add_argument("--dry-run", action="store_true", help="stampa i messaggi, non invia e non salva lo stato")
    parser.add_argument(
        "--no-bootstrap", action="store_true", help="invia anche gli eventi già presenti al primo avvio"
    )
    parser.add_argument("--state", default="local_state.json", help="file di stato (default local_state.json)")
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.no_bootstrap:
        settings = dataclasses.replace(settings, bootstrap_silent=False)

    if args.dry_run:
        store = MemoryStore(FileStore(args.state).data)
        notifier = DryRunNotifier()
    else:
        store = FileStore(args.state)
        notifier = Telegram(
            os.environ["TORINOALERT_BOT_TOKEN"],
            os.environ["TORINOALERT_CHAT_ID"],
            admin_chat_id=os.environ.get("ADMIN_CHAT_ID", ""),
        )

    while True:
        run(default_sources(settings), store, notifier, settings)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
