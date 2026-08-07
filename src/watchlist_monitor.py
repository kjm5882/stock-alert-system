# ============================================================
# 관심종목 신규 공시 감지 & 알림
# GitHub Actions가 주기적으로 이 파일을 실행: python src/watchlist_monitor.py
#
# 두 종류의 공시를 모두 감지한다:
# 1. 잠정실적(공정공시) - 정식 보고서보다 먼저 나오는 실적 예고. 원문을 파싱해서
#    숫자를 최선을 다해 추정하고, 항상 원문 링크를 함께 보낸다 (검증용).
# 2. 정식 분기/반기/사업보고서 - 표준 API로 정확한 재무제표를 가져와 분석.
#
# 처리한 공시 번호(rcept_no)는 data/last_reports.json 에 기록해서 중복 알림 방지.
# ============================================================
import re
import sys
import os
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.watchlist import WATCHLIST
from src import dart_client, krx_client, metrics, telegram_notify, state, prelim_earnings


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
    if month == "03":
        return year, "11013"
    if month == "09":
        return year, "11014"
    return year, "11013"


def notify_periodic_report(ticker: str, name: str, corp_code: str, disclosure: dict):
    """정식 분기/반기/사업보고서: 표준 API로 정확한 수치를 가져와 분석."""
    year, report_code = parse_report_period(disclosure.get("report_nm", ""), disclosure.get("rcept_dt", ""))

    statement = dart_client.get_financial_statement(corp_code, year, report_code)
    if not statement:
        statement = dart_client.get_financial_statement(corp_code, year, report_code, fs_div="OFS")

    accounts = dart_client.extract_key_accounts(statement)
    ratios = metrics.calc_ratios(accounts)

    trading_day = krx_client.get_latest_trading_day()
    valuation = krx_client.get_valuation(ticker, trading_day)
    price = krx_client.get_price_snapshot(ticker, trading_day)

    link = prelim_earnings.build_viewer_url(disclosure.get("rcept_no", ""))
    report_text = (
        "📗 정식 보고서 공시 (확정치)\n"
        + metrics.format_report(name, accounts, ratios, valuation, price)
        + f"\n\n원문: {link}"
    )
    telegram_notify.send_message(report_text)


def notify_prelim_earnings(ticker: str, name: str, disclosure: dict):
    """잠정실적 공정공시: 원문 파싱을 시도하고, 항상 링크를 함께 보낸다."""
    rcept_no = disclosure.get("rcept_no", "")
    link = prelim_earnings.build_viewer_url(rcept_no)

    text = prelim_earnings.fetch_document_text(rcept_no)
    parsed = prelim_earnings.parse_prelim_earnings(text)

    lines = [f"⚡ 잠정실적 공시 감지: {name}({ticker})"]

    if parsed["parsed"]:
        accounts = {
            "매출액": parsed["매출액"],
            "영업이익": parsed["영업이익"],
            "당기순이익": parsed["당기순이익"],
            "자산총계": None,
            "부채총계": None,
            "자본총계": None,
        }
        ratios = metrics.calc_ratios(accounts)

        lines.append("— 자동 추정치 (⚠️ 표 파싱 결과, 오차 가능) —")
        for k in ("매출액", "영업이익", "당기순이익"):
            v = accounts.get(k)
            if v is not None:
                lines.append(f"  {k}: {v:,.0f} (단위 미확정, 원문 확인 필요)")
        for k, v in ratios.items():
            if v is not None:
                lines.append(f"  {k}: {v}")

        try:
            trading_day = krx_client.get_latest_trading_day()
            valuation = krx_client.get_valuation(ticker, trading_day)
            if valuation:
                lines.append("— 밸류에이션 —")
                for k, v in valuation.items():
                    if v is not None:
                        lines.append(f"  {k}: {v}")
        except Exception:
            pass
    else:
        lines.append("자동 숫자 추출에 실패했습니다. 아래 원문 링크에서 직접 확인해주세요.")

    lines.append(f"\n📎 원문(정확한 수치 확인용): {link}")
    telegram_notify.send_message("\n".join(lines))


def run():
    if not WATCHLIST:
        msg = (
            "⚙️ stock-alert-system 구조 점검 실행\n"
            "현재 config/watchlist.py 에 등록된 종목이 없습니다."
        )
        print(msg)
        telegram_notify.send_message(msg)
        return

    seen = state.load_state()  # {ticker: [처리한 rcept_no, ...]}
    today = datetime.now()
    bgn_de = (today - timedelta(days=5)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    any_update = False

    for item in WATCHLIST:
        ticker = item["ticker"]
        name = item["name"]
        processed_for_ticker = set(seen.get(ticker, []))

        try:
            corp_code = dart_client.get_corp_code(ticker)
            if not corp_code:
                telegram_notify.send_message(f"⚠️ {name}({ticker}): DART corp_code를 찾지 못했습니다.")
                continue

            disclosures = dart_client.get_recent_disclosures(corp_code, bgn_de, end_de)
            new_disclosures = [
                d for d in disclosures if d.get("rcept_no") not in processed_for_ticker
            ]

            if not new_disclosures:
                continue

            new_disclosures.sort(key=lambda d: d.get("rcept_dt", ""))

            for disclosure in new_disclosures:
                if disclosure.get("kind") == "periodic":
                    notify_periodic_report(ticker, name, corp_code, disclosure)
                else:
                    notify_prelim_earnings(ticker, name, disclosure)

                processed_for_ticker.add(disclosure.get("rcept_no"))
                any_update = True

            seen[ticker] = list(processed_for_ticker)[-20:]

        except Exception as e:
            telegram_notify.send_message(f"❌ {name}({ticker}) 처리 중 오류: {e}")

    if any_update:
        state.save_state(seen)
    else:
        print("새로운 공시가 없습니다. (정상)")


if __name__ == "__main__":
    run()
