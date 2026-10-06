"""
Prepara build/lambda: codice in src/ + dipendenze di requirements.txt.
Terraform lo zippa e lo carica. Le dipendenze sono Python puro, quindi la build
funziona da Windows/macOS/Linux per una Lambda arm64.

  python scripts/build_lambda.py
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "lambda"
# File che devono esistere nel pacchetto: se manca qualcosa la build fallisce qui, non in produzione.
REQUIRED = ["handler.py", "torinoalert/runner.py", "bs4/__init__.py", "soupsieve/__init__.py",
            "feedparser/__init__.py", "sgmllib.py", "tzdata/__init__.py"]


def _clean(path: Path) -> None:
    """Svuota la build. Su OneDrive/Windows le cartelle possono restare bloccate per un attimo."""
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.5)
    leftovers = [f for f in path.rglob("*") if f.is_file()]
    if leftovers:
        raise RuntimeError(f"Impossibile pulire {path}: {leftovers[:3]}")


def main() -> None:
    _clean(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    shutil.copy2(ROOT / "src" / "handler.py", BUILD / "handler.py")
    shutil.copytree(
        ROOT / "src" / "torinoalert",
        BUILD / "torinoalert",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,  # cartelle vuote rimaste da _clean
    )
    # pip installa in una cartella temporanea (fuori da OneDrive): su una cartella con
    # residui salterebbe i pacchetti "già presenti" lasciando la build senza dipendenze.
    with tempfile.TemporaryDirectory() as deps:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "-r", str(ROOT / "requirements.txt"),
            "--target", deps,
            "--no-compile", "--quiet", "--disable-pip-version-check",
        ])
        shutil.copytree(deps, BUILD, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "bin"))

    missing = [f for f in REQUIRED if not (BUILD / f).is_file()]
    if missing:
        raise RuntimeError(f"Build incompleta, mancano: {missing}")

    print(f"Build pronta in {BUILD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
