# ============================================================
# 대시보드 초기 구축 (수동 1회 실행)
# 1. 20개 종목 각각의 과거 2개년치 분기 실적을 백필 (TTM 계산 재료 확보)
# 2. 각 종목의 최신 정기보고서 기준 스냅샷을 만들어 docs/data/dashboard.json 저장
#
# 실행: python src/dashboard_init.py
# ============================================================
import sys
import os
import json
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.dashboard_stocks import DASHBOARD_STOCKS
from src import dart_client, state, telegram_notify
from src.watchlist_monitor import record_quarter_from_periodic
from src.dashboard import build_stock_record

QUARTERS_STATE_FILE = "docs/data/dashboard_quarters.json"
DASHBOARD_FILE = "docs/data/dashboard.json"


def generate_periods():
    this_year = datetime.now().year
    periods = []
    for year in (this_year - 1, this_year):
        for report_code in ("11013", "11012", "11014", "11011"):
            periods.append((str(year), report_code))
    return periods


def backfill_ticker(ticker: str, quarters_state: dict):
    corp_code = dart_client.get_corp_code(ticker)
    if not corp_code:
        return
    for year, report_code in generate_periods():
        try:
            statement = dart_client.get_financial_statement(corp_code, year, report_code)
            if not statement:
                statement = dart_client.get_financial_statement(corp_code, year, report_code, fs_div="OFS")
            if not statement:
                continue
            accounts = dart_client.extract_key_accounts(statement)
            record_quarter_from_periodic(quarters_state, ticker, year, report_code, accounts.get("당기순이익"))
        except Exception as e:
            print(f"  {ticker} {year} {report_code} 백필 중 오류: {e}")


def run():
    quarters_state = state.load_state(QUARTERS_STATE_FILE)

    records = []
    for item in DASHBOARD_STOCKS:
        print(f"{item['name']}({item['ticker']}) 처리 중...")
        backfill_ticker(item["ticker"], quarters_state)
        record = build_stock_record(item, quarters_state)
        records.append(record)

    state.save_state(quarters_state, QUARTERS_STATE_FILE)

    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.now().isoformat(), "stocks": records}, f, ensure_ascii=False, indent=2)

    ok_count = sum(1 for r in records if "error" not in r)
    msg = f"🧱 대시보드 초기 구축 완료: {ok_count}/{len(records)}개 종목 정상 처리"
    print(msg)
    telegram_notify.send_message(msg)


if __name__ == "__main__":
    run()
