# ============================================================
# 과거 분기 실적 백필(backfill)
# GitHub Actions에서 수동으로 1회 실행: python src/backfill_quarters.py
#
# 관심종목마다 최근 2개년치 정기공시(사업/반기/분기보고서)를 순서대로 가져와서
# watchlist_monitor.py와 똑같은 로직(누적값에서 직전 분기를 빼는 방식)으로
# 분기별 단일 순이익을 역산해 data/last_reports.json 에 채워 넣는다.
#
# 이미 있는 관심종목에 대해 TTM(최근 4개분기) 계산을 "1년 기다리지 않고"
# 바로 가능하게 만들기 위한 초기화용 스크립트다. 여러 번 실행해도 안전하다
# (같은 값으로 덮어쓸 뿐이라 중복 문제 없음).
# ============================================================
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.watchlist import WATCHLIST
from src import dart_client, state, telegram_notify
from src.watchlist_monitor import record_quarter_from_periodic, REPORT_CODE_INFO


def generate_periods():
    """최근 2개년치 (연도, report_code)를 오래된 순서대로 생성한다 (역산에 순서가 중요함)."""
    this_year = datetime.now().year
    periods = []
    for year in (this_year - 1, this_year):
        for report_code in ("11013", "11012", "11014", "11011"):
            periods.append((str(year), report_code))
    return periods


def backfill_ticker(ticker: str, name: str, seen: dict) -> int:
    corp_code = dart_client.get_corp_code(ticker)
    if not corp_code:
        print(f"  {name}({ticker}): corp_code 못 찾음, 스킵")
        return 0

    filled = 0
    for year, report_code in generate_periods():
        try:
            statement = dart_client.get_financial_statement(corp_code, year, report_code)
            if not statement:
                statement = dart_client.get_financial_statement(corp_code, year, report_code, fs_div="OFS")
            if not statement:
                continue  # 아직 안 나온 보고서(미래) 이거나 데이터 없음

            accounts = dart_client.extract_key_accounts(statement)
            net_income = accounts.get("당기순이익")
            before = len(state.get_last_n_quarters(seen, ticker, n=99))
            record_quarter_from_periodic(seen, ticker, year, report_code, net_income)
            after = len(state.get_last_n_quarters(seen, ticker, n=99))
            if after > before:
                filled += 1

        except Exception as e:
            print(f"  {name}({ticker}) {year} {report_code} 처리 중 오류: {e}")

    return filled


def run():
    if not WATCHLIST:
        print("watchlist가 비어있어 백필할 종목이 없습니다.")
        return

    seen = state.load_state()
    summary_lines = ["🧱 과거 분기 실적 백필 결과"]

    for item in WATCHLIST:
        ticker = item["ticker"]
        name = item["name"]
        print(f"{name}({ticker}) 처리 중...")
        filled = backfill_ticker(ticker, name, seen)
        total = len(state.get_last_n_quarters(seen, ticker, n=99))
        summary_lines.append(f"• {name}({ticker}): 이번에 {filled}개 분기 채움 (누적 보유 {total}개 분기)")

    state.save_state(seen)

    message = "\n".join(summary_lines)
    print(message)
    telegram_notify.send_message(message)


if __name__ == "__main__":
    run()
