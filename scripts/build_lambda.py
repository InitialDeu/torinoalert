"""
Prepara build/lambda: codice in src/ + dipendenze di requirements.txt.
Terraform lo zippa e lo carica. Le dipendenze sono Python puro, quindi la build
funziona da Windows/macOS/Linux per una Lambda arm64.

  python scripts/build_lambda.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "lambda"


def main() -> None:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)

    shutil.copy2(ROOT / "src" / "handler.py", BUILD / "handler.py")
    shutil.copytree(
        ROOT / "src" / "torinoalert",
        BUILD / "torinoalert",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "-r", str(ROOT / "requirements.txt"),
        "--target", str(BUILD),
        "--no-compile", "--quiet", "--disable-pip-version-check",
    ])
    for junk in [*BUILD.rglob("__pycache__"), BUILD / "bin"]:
        shutil.rmtree(junk, ignore_errors=True)

    print(f"Build pronta in {BUILD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
