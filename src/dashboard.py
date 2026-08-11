# ============================================================
# 대시보드 핵심 로직
# - 종목의 "현재 시점에서 가장 최근에 나온" 정기보고서를 찾아 재무데이터를 가져온다
# - watchlist_monitor.py의 record_quarter_from_periodic / compute_ttm_valuation을
#   그대로 재사용해서 TTM(최근 4개분기) 기준 밸류에이션을 계산한다
# ============================================================
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


def _period_sort_key(year: str, report_code: str) -> int:
    return int(year) * 100 + REPORT_END_MONTH[report_code]


def find_latest_available_report(corp_code: str):
    """
    현재 시점 기준으로 이미 제출되어 있는 가장 최근 정기보고서를 찾는다.
    최근 2개년치를 최신순으로 시도해서, 실제로 데이터가 있는 첫 번째 것을 반환한다.
    반환값: (year, report_code, statement) 못 찾으면 (None, None, [])
    """
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


def build_stock_record(item: dict, quarters_state: dict) -> dict:
    """
    관심종목 하나에 대해 대시보드에 표시할 레코드를 만든다.
    quarters_state는 TTM 계산을 위해 옆에서 계속 누적되는 상태(dict)이며,
    호출부에서 마지막에 한 번에 저장(save)하면 된다.
    """
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

    # 분기별 단일 실적 기록 갱신 (TTM 계산 재료)
    record_quarter_from_periodic(quarters_state, ticker, year, report_code, accounts.get("당기순이익"))

    trading_day = krx_client.get_latest_trading_day()
    price = krx_client.get_price_snapshot(ticker, trading_day)
    market_cap = price.get("시가총액")

    ttm_result, ttm_note = compute_ttm_valuation(quarters_state, ticker, market_cap)

    # PBR은 KRX의 오래된 사업보고서 기준 대신, 최신 자본총계로 직접 계산 (최신성 확보)
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
        "PBR": pbr_custom,
        "시가총액": market_cap,
        "updated_at": datetime.now().isoformat(),
    }
    return record
