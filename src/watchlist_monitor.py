import re
import sys
import os
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.watchlist import WATCHLIST
from src import dart_client, krx_client, metrics, telegram_notify, state


def parse_report_period(report_nm: str, rcept_dt: str):
    """
    공시 제목과 접수일자로부터 (사업연도, reprt_code)를 추정한다.
    예: "분기보고서 (2026.09)" -> ("2026", "11014")
    """
    match = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
    if match:
        year, month = match.group(1), match.group(2)
    else:
        year, month = rcept_dt[:4], rcept_dt[4:6]

    if "사업보고서" in report_nm:
        return year, "11011"
    if "반기보고서" in report_nm:
        return year, "11012"
    # 분기보고서: 3월말(1분기) vs 9월말(3분기)
    if month == "03":
        return year, "11013"
    if month == "09":
        return year, "11014"
    # 예외적인 경우 월 기준으로 최대한 추정
    return year, "11013"


def analyze_and_notify(ticker: str, name: str, corp_code: str, year: str, report_code: str, is_new: bool):
    statement = dart_client.get_financial_statement(corp_code, year, report_code)
    if not statement:
        statement = dart_client.get_financial_statement(corp_code, year, report_code, fs_div="OFS")

    accounts = dart_client.extract_key_accounts(statement)
    ratios = metrics.calc_ratios(accounts)

    trading_day = krx_client.get_latest_trading_day()
    valuation = krx_client.get_valuation(ticker, trading_day)
    price = krx_client.get_price_snapshot(ticker, trading_day)

    prefix = "🆕 신규 실적 공시" if is_new else "📊"
    report_text = f"{prefix}\n" + metrics.format_report(name, accounts, ratios, valuation, price)
    telegram_notify.send_message(report_text)


def run():
    if not WATCHLIST:
        msg = (
            "⚙️ stock-alert-system 구조 점검 실행\n"
            "현재 config/watchlist.py 에 등록된 종목이 없습니다.\n"
            "종목을 추가하면 신규 공시가 뜰 때마다 자동으로 알림이 옵니다."
        )
        print(msg)
        telegram_notify.send_message(msg)
        return

    seen = state.load_state()
    today = datetime.now()
    bgn_de = (today - timedelta(days=5)).strftime("%Y%m%d")  # 혹시 놓쳤을까봐 5일 여유
    end_de = today.strftime("%Y%m%d")

    any_update = False

    for item in WATCHLIST:
        ticker = item["ticker"]
        name = item["name"]

        try:
            corp_code = dart_client.get_corp_code(ticker)
            if not corp_code:
                telegram_notify.send_message(f"⚠️ {name}({ticker}): DART corp_code를 찾지 못했습니다.")
                continue

            disclosures = dart_client.get_recent_disclosures(corp_code, bgn_de, end_de)
            if not disclosures:
                continue  # 새 공시 없음, 조용히 넘어감

            # 가장 최근 공시 하나만 처리 (같은 기간에 정정 등 여러 건 있을 수 있어 최신 것 기준)
            latest = sorted(disclosures, key=lambda d: d.get("rcept_dt", ""))[-1]
            rcept_no = latest.get("rcept_no")

            if seen.get(ticker) == rcept_no:
                continue  # 이미 알림 보낸 공시

            year, report_code = parse_report_period(latest.get("report_nm", ""), latest.get("rcept_dt", ""))
            analyze_and_notify(ticker, name, corp_code, year, report_code, is_new=True)

            seen[ticker] = rcept_no
            any_update = True

        except Exception as e:
            telegram_notify.send_message(f"❌ {name}({ticker}) 처리 중 오류: {e}")

    if any_update:
        state.save_state(seen)
    else:
        print("새로운 공시가 없습니다. (정상)")


if __name__ == "__main__":
    run()
