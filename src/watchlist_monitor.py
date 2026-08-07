# ============================================================
# 관심종목 신규 공시 감지 & 알림
# GitHub Actions가 주기적으로 이 파일을 실행: python src/watchlist_monitor.py
#
# ※ 당기순이익은 "지배주주 귀속 당기순이익" 기준입니다 (자회사 소수지분 제외).
# ============================================================
import re
import sys
import os
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.watchlist import WATCHLIST
from src import dart_client, krx_client, metrics, telegram_notify, state, prelim_earnings

REPORT_CODE_QUARTER = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def parse_report_period(report_nm: str, rcept_dt: str):
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


def guess_prelim_quarter(report_nm: str, rcept_dt: str):
    q_match = re.search(r"(\d{4})년\s*(\d)\s*분기", report_nm)
    if q_match:
        return q_match.group(1), int(q_match.group(2))

    p_match = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
    if p_match:
        year, month = p_match.group(1), p_match.group(2)
        quarter_map = {"03": 1, "06": 2, "09": 3, "12": 4}
        if month in quarter_map:
            return year, quarter_map[month]

    return None, None


def record_quarter_from_periodic(seen: dict, ticker: str, year: str, report_code: str, cumulative_net_income):
    """
    정기공시의 '누적' 순이익에서, 이미 저장되어 있는 그 해 이전 분기들의 '단일 분기'
    합계를 빼서 이번 분기의 단일 순이익을 역산한다.

    안전장치: 이전 분기들이 하나라도 저장되어 있지 않으면 역산하지 않고 건너뛴다.
    (예: 3분기 데이터가 없는 상태에서 연간보고서를 억지로 "연간 - 0"으로 계산해
    연간 전체 금액이 그대로 4분기 값으로 잘못 들어가는 사고를 방지한다.)
    """
    if cumulative_net_income is None:
        return

    quarter_num = REPORT_CODE_QUARTER[report_code]

    if quarter_num == 1:
        standalone = cumulative_net_income
    else:
        existing = state.get_quarters_for_ticker(seen, ticker)
        needed_keys = [f"{year}Q{i}" for i in range(1, quarter_num)]
        if not all(k in existing for k in needed_keys):
            return  # 이전 분기 데이터가 아직 없어 역산 불가 - 건너뜀 (틀린 값 저장 방지)
        prior_sum = sum(existing[k] for k in needed_keys)
        standalone = cumulative_net_income - prior_sum

    state.set_quarter(seen, ticker, f"{year}Q{quarter_num}", standalone, source="periodic")


def compute_ttm_valuation(seen: dict, ticker: str, market_cap):
    last4 = state.get_last_n_quarters(seen, ticker, n=4)

    if len(last4) < 4:
        have = ", ".join(k for k, _ in last4) if last4 else "없음"
        return None, f"⏭️ 생략: 최근 4개 분기 데이터가 아직 다 모이지 않았습니다 (현재 확보: {have})"

    ttm_net_income = sum(v for _, v in last4)
    if ttm_net_income <= 0 or not market_cap:
        return None, "⏭️ 생략: TTM 순이익이 0 이하이거나 시가총액 조회에 실패했습니다"

    per_ttm = market_cap / ttm_net_income
    quarter_range = f"{last4[0][0]} ~ {last4[-1][0]}"
    return {"ttm_net_income": ttm_net_income, "per_ttm": per_ttm, "range": quarter_range}, None


def format_valuation_sections(ttm_result, ttm_note, official_valuation: dict) -> list:
    lines = ["— 밸류에이션(잠정, 최근 4개분기 TTM · 지배주주 순이익 기준) —"]
    if ttm_result:
        lines.append(f"  기준 분기: {ttm_result['range']}")
        lines.append(f"  TTM 순이익(지배주주): {ttm_result['ttm_net_income']:,.0f}원")
        lines.append(f"  PER(잠정): {ttm_result['per_ttm']:.2f}")
    else:
        lines.append(f"  {ttm_note}")

    lines.append("— 밸류에이션(확정, KRX 제공 · 전년도 사업보고서 기준) —")
    if official_valuation:
        for k, v in official_valuation.items():
            if v is not None:
                lines.append(f"  {k}: {v}")
    else:
        lines.append("  조회 실패")

    return lines


def notify_periodic_report(ticker: str, name: str, corp_code: str, disclosure: dict, seen: dict):
    year, report_code = parse_report_period(disclosure.get("report_nm", ""), disclosure.get("rcept_dt", ""))

    statement = dart_client.get_financial_statement(corp_code, year, report_code)
    if not statement:
        statement = dart_client.get_financial_statement(corp_code, year, report_code, fs_div="OFS")

    accounts = dart_client.extract_key_accounts(statement)
    ratios = metrics.calc_ratios(accounts)

    record_quarter_from_periodic(seen, ticker, year, report_code, accounts.get("당기순이익"))

    trading_day = krx_client.get_latest_trading_day()
    price = krx_client.get_price_snapshot(ticker, trading_day)
    official_valuation = krx_client.get_valuation(ticker, trading_day)
    ttm_result, ttm_note = compute_ttm_valuation(seen, ticker, price.get("시가총액"))

    link = prelim_earnings.build_viewer_url(disclosure.get("rcept_no", ""))

    lines = ["📗 정식 보고서 공시 (확정치)", f"{name}({ticker})", "※ 당기순이익은 지배주주 귀속 기준"]
    lines.append("— 실적 —")
    for k, v in accounts.items():
        if v is not None:
            lines.append(f"  {k}: {v:,}원")
    lines.append("— 재무비율 —")
    for k, v in ratios.items():
        if v is not None:
            lines.append(f"  {k}: {v}")
    lines.extend(format_valuation_sections(ttm_result, ttm_note, official_valuation))
    lines.append(f"\n원문: {link}")

    telegram_notify.send_message("\n".join(lines))


def notify_prelim_earnings(ticker: str, name: str, disclosure: dict, seen: dict):
    rcept_no = disclosure.get("rcept_no", "")
    link = prelim_earnings.build_viewer_url(rcept_no)

    text = prelim_earnings.fetch_document_text(rcept_no)
    parsed = prelim_earnings.parse_prelim_earnings(text)

    lines = [f"⚡ 잠정실적 공시 감지: {name}({ticker})", "※ 당기순이익은 지배주주 귀속 기준(가능한 경우)"]

    if not parsed["parsed"]:
        lines.append("자동 숫자 추출에 실패했습니다. 아래 원문 링크에서 직접 확인해주세요.")
        lines.append(f"\n📎 원문(정확한 수치 확인용): {link}")
        telegram_notify.send_message("\n".join(lines))
        return

    unit = parsed["unit"]
    unit_label = unit if unit else "단위 확인 불가 ⚠️"

    lines.append(f"— 자동 추정치 (⚠️ 표 파싱 결과, 오차 가능 / 단위: {unit_label}) —")
    for k in ("매출액", "영업이익", "당기순이익"):
        v = parsed.get(k)
        if v is not None:
            lines.append(f"  {k}: {v:,.0f}{unit or ''}")

    accounts = {
        "매출액": parsed["매출액"], "영업이익": parsed["영업이익"], "당기순이익": parsed["당기순이익"],
        "자산총계": None, "부채총계": None, "자본총계": None,
    }
    ratios = metrics.calc_ratios(accounts)
    lines.append("— 재무비율(추정) —")
    for k, v in ratios.items():
        if v is not None:
            lines.append(f"  {k}: {v}")

    net_income_won = prelim_earnings.to_won(parsed["당기순이익"], unit)
    year, quarter_num = guess_prelim_quarter(disclosure.get("report_nm", ""), disclosure.get("rcept_dt", ""))
    if net_income_won is not None and year and quarter_num:
        state.set_quarter(seen, ticker, f"{year}Q{quarter_num}", net_income_won, source="prelim")

    try:
        trading_day = krx_client.get_latest_trading_day()
        price = krx_client.get_price_snapshot(ticker, trading_day)
        official_valuation = krx_client.get_valuation(ticker, trading_day)
        ttm_result, ttm_note = compute_ttm_valuation(seen, ticker, price.get("시가총액"))
        lines.extend(format_valuation_sections(ttm_result, ttm_note, official_valuation))
    except Exception as e:
        lines.append(f"— 밸류에이션 조회 중 오류: {e} —")

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

    seen = state.load_state()
    today = datetime.now()
    bgn_de = (today - timedelta(days=5)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    any_update = False

    for item in WATCHLIST:
        ticker = item["ticker"]
        name = item["name"]
        processed_for_ticker = set(state.get_processed(seen, ticker))

        try:
            corp_code = dart_client.get_corp_code(ticker)
            if not corp_code:
                telegram_notify.send_message(f"⚠️ {name}({ticker}): DART corp_code를 찾지 못했습니다.")
                continue

            disclosures = dart_client.get_recent_disclosures(corp_code, bgn_de, end_de)
            new_disclosures = [d for d in disclosures if d.get("rcept_no") not in processed_for_ticker]

            if not new_disclosures:
                continue

            new_disclosures.sort(key=lambda d: d.get("rcept_dt", ""))

            for disclosure in new_disclosures:
                if disclosure.get("kind") == "periodic":
                    notify_periodic_report(ticker, name, corp_code, disclosure, seen)
                else:
                    notify_prelim_earnings(ticker, name, disclosure, seen)

                processed_for_ticker.add(disclosure.get("rcept_no"))
                any_update = True

            state.set_processed(seen, ticker, list(processed_for_ticker)[-20:])

        except Exception as e:
            telegram_notify.send_message(f"❌ {name}({ticker}) 처리 중 오류: {e}")

    if any_update:
        state.save_state(seen)
    else:
        print("새로운 공시가 없습니다. (정상)")


if __name__ == "__main__":
    run()
