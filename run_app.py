import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

def ensure_demo_data():
    dbfile = ROOT / "construction_ai.db"
    if dbfile.exists():
        return

    result = subprocess.run(
        [sys.executable, "seed_live_demo.py"],
        cwd=ROOT
    )

    if result.returncode != 0:
        raise SystemExit(result.returncode)

if __name__ == "__main__":
    ensure_demo_data()

    port = os.getenv("PORT", "8000")

    os.execvp(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "full_app:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
    )
