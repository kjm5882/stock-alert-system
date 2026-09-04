"""
pykrx 진단 스크립트 (임시)
=========================
GitHub Actions 환경에서 pykrx의 어떤 함수가 작동하고 어떤 게 안 되는지 확인.
이 결과를 보고 수급 담당자를 어떻게 고칠지 결정합니다.
"""

from pykrx import stock
from datetime import datetime, timedelta

# 최근 영업일 근처 날짜 범위
end = datetime.now().strftime("%Y%m%d")
start = (datetime.now() - timedelta(days=20)).strftime("%Y%m%d")

print("=" * 60)
print(f"테스트 기간: {start} ~ {end}")
print("=" * 60)

tests = [
    ("1. get_market_ticker_list (종목목록)",
     lambda: stock.get_market_ticker_list(end, market="KOSPI")),

    ("2. get_market_ticker_name (종목명, 삼성전자)",
     lambda: stock.get_market_ticker_name("005930")),

    ("3. get_market_ohlcv (개별종목 시세)",
     lambda: stock.get_market_ohlcv(start, end, "005930")),

    ("4. get_market_fundamental (PER/PBR)",
     lambda: stock.get_market_fundamental(start, end, "005930")),

    ("5. get_market_trading_value_by_date (수급 - 핵심!)",
     lambda: stock.get_market_trading_value_by_date(start, end, "005930")),

    ("6. get_market_cap (시가총액)",
     lambda: stock.get_market_cap(start, end, "005930")),
]

for name, fn in tests:
    print(f"\n{name}")
    try:
        result = fn()
        if result is None:
            print("   ❌ None 반환")
        elif isinstance(result, str):
            print(f"   ✅ 성공: {result}")
        elif hasattr(result, "empty"):
            if result.empty:
                print("   ❌ 빈 DataFrame")
            else:
                print(f"   ✅ 성공: {len(result)}행")
                print(f"   컬럼: {list(result.columns)}")
                print(f"   마지막 행:\n{result.tail(1)}")
        elif isinstance(result, list):
            if not result:
                print("   ❌ 빈 리스트")
            else:
                print(f"   ✅ 성공: {len(result)}개, 샘플 {result[:3]}")
        else:
            print(f"   ✅ 성공: {result}")
    except Exception as e:
        print(f"   ❌ 에러: {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print("진단 완료")
print("=" * 60)
