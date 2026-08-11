# ============================================================
# 주가/차트 전용 일일 갱신
# GitHub Actions가 매일 실행: python src/dashboard_price_refresh.py
#
# 재무 데이터(매출/영업이익 등)는 건드리지 않고,
# 시가총액/종가/등락률/최근 1년 주가 히스토리만 갱신한다.
# (DART 호출이 필요 없어서 dashboard_monitor.py보다 훨씬 가볍고 빠르다)
# ============================================================
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.dashboard_stocks import DASHBOARD_STOCKS
from src import krx_client
from src.dashboard import load_dashboard, save_dashboard


def run():
    dashboard = load_dashboard()
    existing_by_ticker = {r["ticker"]: r for r in dashboard.get("stocks", [])}

    trading_day = krx_client.get_latest_trading_day()

    for item in DASHBOARD_STOCKS:
        ticker = item["ticker"]
        try:
            price = krx_client.get_price_snapshot(ticker, trading_day)
            price_history = krx_client.get_price_history(ticker, days=365)

            record = existing_by_ticker.get(ticker, {**item})
            record.update({
                "시가총액": price.get("시가총액", record.get("시가총액")),
                "종가": price.get("종가", record.get("종가")),
                "등락률": price.get("등락률", record.get("등락률")),
                "price_history": price_history or record.get("price_history", []),
            })
            existing_by_ticker[ticker] = record

        except Exception as e:
            print(f"{item['name']}({ticker}) 주가 갱신 중 오류: {e}")

    ordered_records = [existing_by_ticker.get(item["ticker"], item) for item in DASHBOARD_STOCKS]
    save_dashboard({"updated_at": datetime.now().isoformat(), "stocks": ordered_records})
    print("주가/차트 갱신 완료")


if __name__ == "__main__":
    run()
