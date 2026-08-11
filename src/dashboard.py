# ============================================================
# 대시보드 핵심 로직
# - 종목의 "현재 시점에서 가장 최근에 나온" 정기보고서를 찾아 재무데이터를 가져온다
# - watchlist_monitor.py의 record_quarter_from_periodic / compute_ttm_valuation을
#   그대로 재사용해서 TTM(최근 4개분기) 기준 밸류에이션을 계산한다
# - 최근 1년 종가 히스토리도 함께 담아서, 웹페이지에서 간이차트로 표시한다
# ============================================================
import json
import os
from datetime import datetime

from src import dart_client, krx_client, metrics, state
from src.watchlist_monitor import record_quarter_from_periodic, compute_ttm_valuation

REPORT_CODE_LABEL = {
    "11013": "1분기보고서",
    "11012": "반기보고서",
    "11014": "3분기보고서",
    "11011": "사업보고서",
}
REPORT_END_MONTH = {"11013": 3, "11012": 6, "11014": 9, "11011": 12}

DASHBOARD_FILE = "docs/data/dashboard.json"


def load_dashboard() -> dict:
    if not os.path.exists(DASHBOARD_FILE):
        return {"updated_at": None, "stocks": []}
    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_dashboard(data: dict):
    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _period_sort_key(year: str, report_code: str) -> int:
    return int(year) * 100 + REPORT_END_MONTH[report_code]


def find_latest_available_report(corp_code: str):
    """현재 시점 기준으로 이미 제출되어 있는 가장 최근 정기보고서를 찾는다."""
    this_year = datetime.now().year
    candidates = [
        (str(y), c)
        for y in (this_year, this_year - 1)
        for c in ("11013", "11012", "11014", "11011")
    ]
    candidates.sort(key=lambda yc: _period_sort_key(*yc), reverse=True)

    for year, report_code in candidates:
        statement = dart_client.get_financial_statement(corp_code, year, report_code)
        if not statement:
            statement = dart_client.get_financial_statement(corp_code, year, report_code, fs_div="OFS")
        if statement:
            return year, report_code, statement

    return None, None, []


def compute_forward_per(quarters_state: dict, ticker: str, market_cap):
    """
    최근 2개 분기 합계를 2배(연환산)해서 forward PER을 계산한다.
    TTM(최근 4개분기)과 달리, 가장 최근 흐름만 반영해서 더 민감하게 움직인다.
    """
    last2 = state.get_last_n_quarters(quarters_state, ticker, n=2)

    if len(last2) < 2:
        have = ", ".join(k for k, _ in last2) if last2 else "없음"
        return None, f"⏭️ 생략: 최근 2개 분기 데이터가 아직 다 모이지 않았습니다 (현재 확보: {have})"

    two_q_sum = sum(v for _, v in last2)
    annualized_net_income = two_q_sum * 2

    if annualized_net_income <= 0 or not market_cap:
        return None, "⏭️ 생략: 연환산 순이익이 0 이하이거나 시가총액 조회에 실패했습니다"

    forward_per = market_cap / annualized_net_income
    quarter_range = f"{last2[0][0]} ~ {last2[-1][0]} ×2"
    return {"annualized_net_income": annualized_net_income, "forward_per": forward_per, "range": quarter_range}, None


def build_stock_record(item: dict, quarters_state: dict) -> dict:
    """관심종목 하나에 대해 대시보드에 표시할 레코드(재무+밸류에이션+차트)를 만든다."""
    ticker = item["ticker"]
    name = item["name"]

    corp_code = dart_client.get_corp_code(ticker)
    if not corp_code:
        return {**item, "error": "corp_code를 찾지 못했습니다.", "updated_at": datetime.now().isoformat()}

    year, report_code, statement = find_latest_available_report(corp_code)
    if not statement:
        return {**item, "error": "정기보고서 데이터를 찾지 못했습니다.", "updated_at": datetime.now().isoformat()}

    accounts = dart_client.extract_key_accounts(statement)
    ratios = metrics.calc_ratios(accounts)

    record_quarter_from_periodic(quarters_state, ticker, year, report_code, accounts.get("당기순이익"))

    trading_day = krx_client.get_latest_trading_day()
    price = krx_client.get_price_snapshot(ticker, trading_day)
    market_cap = price.get("시가총액")
    price_history = krx_client.get_price_history(ticker, days=365)

    ttm_result, ttm_note = compute_ttm_valuation(quarters_state, ticker, market_cap)
    forward_result, forward_note = compute_forward_per(quarters_state, ticker, market_cap)

    equity = accounts.get("자본총계")
    pbr_custom = None
    if equity and equity > 0 and market_cap:
        pbr_custom = round(market_cap / equity, 2)

    record = {
        "ticker": ticker,
        "name": name,
        "category": item.get("category"),
        "business": item.get("business"),
        "기준보고서": f"{year} {REPORT_CODE_LABEL.get(report_code, report_code)}",
        "매출액": accounts.get("매출액"),
        "영업이익": accounts.get("영업이익"),
        "당기순이익": accounts.get("당기순이익"),
        "영업이익률": ratios.get("영업이익률(%)"),
        "부채비율": ratios.get("부채비율(%)"),
        "PER_TTM": round(ttm_result["per_ttm"], 2) if ttm_result else None,
        "PER_TTM_기준분기": ttm_result["range"] if ttm_result else None,
        "PER_비고": None if ttm_result else ttm_note,
        "PER_Forward": round(forward_result["forward_per"], 2) if forward_result else None,
        "PER_Forward_기준분기": forward_result["range"] if forward_result else None,
        "PER_Forward_비고": None if forward_result else forward_note,
        "PBR": pbr_custom,
        "시가총액": market_cap,
        "종가": price.get("종가"),
        "등락률": price.get("등락률"),
        "price_history": price_history,
        "updated_at": datetime.now().isoformat(),
    }
    return record
