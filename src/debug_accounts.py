# ============================================================
# 진단용: DART 재무제표 응답의 실제 account_nm / account_id / 금액을
# 있는 그대로 출력한다. 정확한 매칭 로직을 만들기 위한 확인용 스크립트.
#
# 실행: python src/debug_accounts.py
# 환경변수 DEBUG_TICKER, DEBUG_YEAR, DEBUG_REPORT_CODE 로 대상을 바꿀 수 있다.
# ============================================================
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dart_client, telegram_notify


def run():
    ticker = os.environ.get("DEBUG_TICKER", "000660")   # 기본값: SK하이닉스
    year = os.environ.get("DEBUG_YEAR", "2025")
    report_code = os.environ.get("DEBUG_REPORT_CODE", "11013")  # 1분기보고서

    corp_code = dart_client.get_corp_code(ticker)
    print(f"ticker={ticker} corp_code={corp_code} year={year} report_code={report_code}")

    statement = dart_client.get_financial_statement(corp_code, year, report_code)
    fs_div_used = "CFS"
    if not statement:
        statement = dart_client.get_financial_statement(corp_code, year, report_code, fs_div="OFS")
        fs_div_used = "OFS"

    print(f"fs_div={fs_div_used}, 총 {len(statement)}개 행\n")

    lines = [f"🔍 진단 결과: {ticker} {year} {report_code} ({fs_div_used})"]

    keywords = ["순이익", "매출", "영업이익"]
    for row in statement:
        name = row.get("account_nm", "")
        if any(kw in name for kw in keywords):
            info = (
                f"sj_div={row.get('sj_div')} | account_id={row.get('account_id')} | "
                f"account_nm={name} | thstrm_amount={row.get('thstrm_amount')}"
            )
            print(info)
            lines.append(info)

    message = "\n".join(lines)
    if len(message) > 3800:
        message = message[:3800] + "\n...(생략, 전체는 Actions 로그에서 확인)"
    telegram_notify.send_message(message)


if __name__ == "__main__":
    run()
