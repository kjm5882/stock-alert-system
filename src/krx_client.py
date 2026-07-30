# ============================================================
# KRX 시세 / 밸류에이션 지표 클라이언트 (pykrx 라이브러리 사용)
# - PER, PBR, 배당수익률
# - 최근 종가, 거래량, 시가총액
# ============================================================
from datetime import datetime, timedelta
from pykrx import stock


def get_latest_trading_day() -> str:
    """가장 최근 거래일(YYYYMMDD)을 찾는다. 주말/공휴일은 데이터가 비어서 최대 7일 전까지 탐색."""
    today = datetime.now()
    for i in range(7):
        day = (today - timedelta(days=i)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv(day, day, "005930")  # 삼성전자로 거래일 여부 확인
        if not df.empty:
            return day
    raise RuntimeError("최근 거래일을 찾을 수 없습니다.")


def get_valuation(ticker: str, date: str) -> dict:
    """PER, PBR, 배당수익률 등을 조회한다."""
    df = stock.get_market_fundamental(date, date, ticker)
    if df.empty:
        return {}
    row = df.iloc[0]
    return {
        "PER": row.get("PER"),
        "PBR": row.get("PBR"),
        "배당수익률": row.get("DIV"),
        "EPS": row.get("EPS"),
        "BPS": row.get("BPS"),
    }


def get_price_snapshot(ticker: str, date: str) -> dict:
    """종가, 등락률, 거래량, 시가총액을 조회한다."""
    ohlcv = stock.get_market_ohlcv(date, date, ticker)
    cap = stock.get_market_cap(date, date, ticker)

    result = {}
    if not ohlcv.empty:
        row = ohlcv.iloc[0]
        result.update({
            "종가": int(row.get("종가", 0)),
            "등락률": float(row.get("등락률", 0)),
            "거래량": int(row.get("거래량", 0)),
        })
    if not cap.empty:
        result["시가총액"] = int(cap.iloc[0].get("시가총액", 0))

    return result
