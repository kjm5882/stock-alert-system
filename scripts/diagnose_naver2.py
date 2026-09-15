"""
네이버 금융 수급 데이터 구조 확인 v2 (임시 진단용)
================================================
1차 진단 결과: 페이지 접근은 200 성공, 그러나 표를 못 찾음.
→ 네이버 금융은 실제 데이터 표를 iframe으로 분리해두는 구조이므로
  iframe 주소를 찾아서 그 페이지를 직접 요청해봅니다.
"""

import re
import requests
import pandas as pd
from io import StringIO
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
}

TEST_TICKER = "005930"

print("=" * 60)
print("네이버 금융 구조 확인 v2 (iframe 추적)")
print("=" * 60)

# ── 1) 겉 페이지에서 iframe 목록 찾기 ──────────────────
url = f"https://finance.naver.com/item/frgn.naver?code={TEST_TICKER}"
print(f"\n[1] 겉 페이지 요청: {url}")
res = requests.get(url, headers=HEADERS, timeout=15)
res.encoding = "euc-kr"
print(f"    상태코드: {res.status_code}")

soup = BeautifulSoup(res.text, "html.parser")
iframes = soup.find_all("iframe")
print(f"    발견된 iframe 개수: {len(iframes)}")
for i, f in enumerate(iframes):
    print(f"      iframe[{i}] src = {f.get('src')}")

# ── 2) iframe 각각을 직접 요청해서 표가 있는지 확인 ────
print("\n[2] 각 iframe 직접 요청")
for i, f in enumerate(iframes):
    src = f.get("src")
    if not src:
        continue
    full_url = src if src.startswith("http") else f"https://finance.naver.com{src}"
    print(f"\n    --- iframe[{i}] {full_url} ---")
    try:
        r = requests.get(full_url, headers=HEADERS, timeout=15)
        r.encoding = "euc-kr"
        print(f"    상태코드: {r.status_code}, 길이: {len(r.text)}")
        try:
            tables = pd.read_html(StringIO(r.text))
            print(f"    표 개수: {len(tables)}")
            for j, t in enumerate(tables):
                if t.shape[0] >= 3:
                    print(f"      표[{j}] {t.shape}")
                    print(f"      컬럼: {list(t.columns)}")
                    print(t.head(4).to_string()[:900])
        except Exception as e:
            print(f"    표 파싱 실패: {e}")
    except Exception as e:
        print(f"    요청 실패: {e}")

# ── 3) 종목 목록 페이지도 동일하게 확인 ────────────────
print("\n\n[3] 시가총액 목록 페이지 구조")
url3 = "https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page=1"
r3 = requests.get(url3, headers=HEADERS, timeout=15)
r3.encoding = "euc-kr"
print(f"    상태코드: {r3.status_code}")
try:
    tables3 = pd.read_html(StringIO(r3.text))
    print(f"    표 개수: {len(tables3)}")
    for j, t in enumerate(tables3):
        print(f"      표[{j}] {t.shape}, 컬럼 {list(t.columns)[:8]}")
except Exception as e:
    print(f"    표 파싱 실패: {e}")
    # 종목 코드 링크라도 뽑아보기
    codes = re.findall(r'code=(\d{6})', r3.text)
    print(f"    페이지에서 발견된 종목코드 개수: {len(set(codes))}")
    print(f"    샘플: {list(set(codes))[:5]}")

print("\n" + "=" * 60)
print("확인 완료")
print("=" * 60)
