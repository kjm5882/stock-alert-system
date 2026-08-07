import json
import os

STATE_FILE = "data/last_reports.json"


def load_state() -> dict:
    """{ticker: 마지막으로 처리한 rcept_no} 형태의 상태를 불러온다."""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
