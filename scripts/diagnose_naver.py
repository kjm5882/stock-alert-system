"""
네이버 금융 수급 데이터 구조 확인 (임시 진단용)
==========================================
GitHub Actions 환경에서 네이버 금융의 종목별 외국인/기관 매매동향 페이지를
실제로 읽어올 수 있는지, 어떤 구조로 오는지 확인합니다.
결과를 보고 수급 담당자 코드를 확정합니다.
"""

import requests
import pandas as pd
from io import StringIO

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
}

TEST_TICKER = "005930"  # 삼성전자

print("=" * 60)
print("네이버 금융 수급 페이지 구조 확인")
print("=" * 60)

# 1) 종목별 외국인/기관 매매동향 페이지
url = f"https://finance.naver.com/item/frgn.naver?code={TEST_TICKER}"
print(f"\n[1] 페이지 요청: {url}")
try:
    res = requests.get(url, headers=HEADERS, timeout=15)
    print(f"    상태코드: {res.status_code}, 응답 길이: {len(res.text)}자")

    if res.status_code == 200:
        res.encoding = "euc-kr"  # 네이버 금융은 euc-kr 인코딩
        tables = pd.read_html(StringIO(res.text))
        print(f"    발견된 표 개수: {len(tables)}")

        for i, table in enumerate(tables):
            print(f"\n    --- 표 {i} ({table.shape[0]}행 x {table.shape[1]}열) ---")
            print(f"    컬럼: {list(table.columns)}")
            if not table.empty:
                print(table.head(3).to_string()[:800])
except Exception as e:
    print(f"    에러: {type(e).__name__}: {e}")

# 2) 종목명 확인용 (매핑에 쓸 수 있는지)
url2 = f"https://finance.naver.com/item/main.naver?code={TEST_TICKER}"
print(f"\n\n[2] 종목 메인 페이지: {url2}")
try:
    res2 = requests.get(url2, headers=HEADERS, timeout=15)
    print(f"    상태코드: {res2.status_code}, 응답 길이: {len(res2.text)}자")
except Exception as e:
    print(f"    에러: {type(e).__name__}: {e}")

# 3) 상장종목 전체 목록 (티커맵 대안)
url3 = "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1"
print(f"\n\n[3] 시가총액 순 종목 목록: {url3}")
try:
    res3 = requests.get(url3, headers=HEADERS, timeout=15)
    print(f"    상태코드: {res3.status_code}, 응답 길이: {len(res3.text)}자")
    if res3.status_code == 200:
        res3.encoding = "euc-kr"
        tables3 = pd.read_html(StringIO(res3.text))
        print(f"    발견된 표 개수: {len(tables3)}")
        for i, t in enumerate(tables3):
            if t.shape[0] > 5:
                print(f"    표 {i}: {t.shape}, 컬럼 {list(t.columns)[:6]}")
except Exception as e:
    print(f"    에러: {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print("확인 완료")
print("=" * 60)
