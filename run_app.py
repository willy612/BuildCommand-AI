import os,sys
port=os.getenv("PORT","8000")
os.execvp(sys.executable,[sys.executable,"-m","uvicorn","full_app:app","--host","0.0.0.0","--port",port])
