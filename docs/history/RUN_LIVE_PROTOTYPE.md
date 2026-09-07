# Run the live prototype

This build includes two UI paths:

## Offline-compatible live prototype
Requires FastAPI + Uvicorn + SQLAlchemy.

    python seed_live_demo.py
    uvicorn live_app:app --host 0.0.0.0 --port 8000

Then open:

    http://localhost:8000

## Full Streamlit product UI
When internet/package installation is available:

    pip install -r requirements.txt
    python seed_live_demo.py
    streamlit run app_v3.py

The FastAPI live prototype exists so the product can be shown without requiring Streamlit.
