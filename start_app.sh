#!/usr/bin/env bash
set -e
python3 -m pip install -r requirements.txt
python3 seed_live_demo.py
python3 run_app.py
