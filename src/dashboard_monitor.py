# ============================================================
# 대시보드 자동 갱신
# GitHub Actions가 주기적으로 실행: python src/dashboard_monitor.py
# 관심종목 알림과 달리, "잠정실적"은 무시하고 정식 정기보고서가 새로 뜬
# 종목만 골라서 해당 종목의 대시보드 레코드를 다시 계산한다.
# ============================================================
import sys
import os
import json
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.dashboard_stocks import DASHBOARD_STOCKS
from src import dart_client, state, telegram_notify
from src.dashboard import build_stock_record

QUARTERS_STATE_FILE = "docs/data/dashboard_quarters.json"
DASHBOARD_FILE = "docs/data/dashboard.json"
PROCESSED_KEY_PREFIX = "dashboard_"  # watchlist용 processed와 이름이 겹치지 않도록 구분


def load_dashboard():
    if not os.path.exists(DASHBOARD_FILE):
        return {"updated_at": None, "stocks": []}
    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_dashboard(data: dict):
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run():
    quarters_state = state.load_state(QUARTERS_STATE_FILE)
    dashboard = load_dashboard()
    existing_by_ticker = {r["ticker"]: r for r in dashboard.get("stocks", [])}

    today = datetime.now()
    bgn_de = (today - timedelta(days=10)).strftime("%Y%m%d")  # 실행 주기가 길 수 있어 여유있게 10일
    end_de = today.strftime("%Y%m%d")

    updated_names = []

    for item in DASHBOARD_STOCKS:
        ticker = item["ticker"]
        name = item["name"]
        processed_key = PROCESSED_KEY_PREFIX + ticker

        try:
            corp_code = dart_client.get_corp_code(ticker)
            if not corp_code:
                continue

            disclosures = dart_client.get_recent_disclosures(corp_code, bgn_de, end_de)
            periodic_only = [d for d in disclosures if d.get("kind") == "periodic"]
            if not periodic_only:
                continue

            processed = set(state.get_processed(quarters_state, processed_key))
            new_ones = [d for d in periodic_only if d.get("rcept_no") not in processed]
            if not new_ones:
                continue

            # 새 정기보고서가 있으면 해당 종목 레코드를 다시 계산
            record = build_stock_record(item, quarters_state)
            existing_by_ticker[ticker] = record
            updated_names.append(name)

            for d in new_ones:
                processed.add(d.get("rcept_no"))
            state.set_processed(quarters_state, processed_key, list(processed)[-20:])

        except Exception as e:
            print(f"{name}({ticker}) 처리 중 오류: {e}")

    if updated_names:
        # DASHBOARD_STOCKS 순서를 유지해서 저장
        ordered_records = [existing_by_ticker.get(item["ticker"], item) for item in DASHBOARD_STOCKS]
        save_dashboard({"updated_at": datetime.now().isoformat(), "stocks": ordered_records})
        state.save_state(quarters_state, QUARTERS_STATE_FILE)

        msg = f"📈 대시보드 갱신: {', '.join(updated_names)} 신규 보고서 반영"
        print(msg)
        telegram_notify.send_message(msg)
    else:
        print("새로운 정기보고서가 없습니다. (정상)")


if __name__ == "__main__":
    run()
