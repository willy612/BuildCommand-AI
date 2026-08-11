import os, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
os.chdir(ROOT)

if __name__=="__main__":
    port=os.getenv("PORT","8000")
    os.execvp(sys.executable,[
        sys.executable,"-m","uvicorn","full_app:app",
        "--host","0.0.0.0","--port",port
    ])
