# ============================================================
# 메인 실행 스크립트
# GitHub Actions가 이 파일을 실행합니다: python src/main.py
# ============================================================
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.watchlist import WATCHLIST, REPORT_CODES
from src import dart_client, krx_client, metrics, telegram_notify


def current_report_code() -> str:
    """현재 월을 기준으로 가장 최근에 나왔을 법한 분기보고서 코드를 추정한다."""
    month = datetime.now().month
    if month in (1, 2, 3, 4):
        return REPORT_CODES["ANNUAL"], datetime.now().year - 1
    elif month in (5, 6, 7):
        return REPORT_CODES["Q1"], datetime.now().year
    elif month in (8, 9, 10):
        return REPORT_CODES["H1"], datetime.now().year
    else:
        return REPORT_CODES["Q3"], datetime.now().year


def run():
    if not WATCHLIST:
        msg = (
            "⚙️ stock-alert-system 구조 점검 실행\n"
            "현재 config/watchlist.py 에 등록된 종목이 없습니다.\n"
            "종목을 추가하면 다음 실행부터 실적/지표 알림이 시작됩니다."
        )
        print(msg)
        telegram_notify.send_message(msg)
        return

    report_code, year = current_report_code()
    trading_day = krx_client.get_latest_trading_day()

    for stock_item in WATCHLIST:
        ticker = stock_item["ticker"]
        name = stock_item["name"]

        try:
            corp_code = dart_client.get_corp_code(ticker)
            if not corp_code:
                telegram_notify.send_message(f"⚠️ {name}({ticker}): DART corp_code를 찾지 못했습니다.")
                continue

            statement = dart_client.get_financial_statement(corp_code, str(year), report_code)
            if not statement:
                # 연결재무제표 없는 회사는 별도재무제표로 재시도
                statement = dart_client.get_financial_statement(corp_code, str(year), report_code, fs_div="OFS")

            accounts = dart_client.extract_key_accounts(statement)
            ratios = metrics.calc_ratios(accounts)
            valuation = krx_client.get_valuation(ticker, trading_day)
            price = krx_client.get_price_snapshot(ticker, trading_day)

            report_text = metrics.format_report(name, accounts, ratios, valuation, price)
            telegram_notify.send_message(report_text)

        except Exception as e:
            telegram_notify.send_message(f"❌ {name}({ticker}) 처리 중 오류: {e}")


if __name__ == "__main__":
    run()
