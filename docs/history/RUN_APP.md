# Run the app

## 1. Install
    pip install -r requirements.txt

## 2. Seed demo data
    python production_bootstrap.py

## 3. Start
    streamlit run app_v3.py

For API work:
    uvicorn api.main:app --reload --port 8000
