"""
네이버 연도별 재무지표 API 탐색 (임시 진단용)
==========================================
목적: 연도별 PER을 직접 제공하는 API가 있는지 확인.
     있으면 과거 PER 계산 없이 바로 쓸 수 있음.
     없으면 (주가 이력 + DART EPS)로 직접 계산해야 함.
"""

import json
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}

TICKER = "005930"

candidates = [
    ("A. 연간 재무제표 (finance/annual)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/finance/annual"),

    ("B. 분기 재무제표 (finance/quarter)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/finance/quarter"),

    ("C. 투자지표 (investmentIndex)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/investmentIndex"),

    ("D. 기업개요/분석 (companyOverview)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/companyOverview"),

    ("E. 컨센서스 (consensus)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/consensus"),

    ("F. 업종비교 (industryCompare)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/industryCompare"),

    ("G. 과거 주가 (chart - 10년치 가능한지)",
     f"https://api.stock.naver.com/chart/domestic/item/{TICKER}/day"
     f"?startDateTime=201601010000&endDateTime=202609160000"),

    ("H. 외부 차트 API (front-api chart)",
     f"https://m.stock.naver.com/front-api/external/chart/domestic/info"
     f"?symbol={TICKER}&requestType=1&startTime=20160101&endTime=20260916&timeframe=year"),
]

print("=" * 70)
print("네이버 연도별 재무지표 API 탐색")
print("=" * 70)

for label, url in candidates:
    print(f"\n{'─' * 70}")
    print(f"{label}")
    print(f"URL: {url[:110]}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        print(f"상태코드: {r.status_code}, 길이: {len(r.text)}")

        if r.status_code == 200 and r.text.strip():
            text = r.text.strip()
            # PER 관련 키워드가 있는지 먼저 확인
            has_per = "PER" in text.upper() or "per" in text
            print(f"PER 관련 문자열 포함: {'예' if has_per else '아니오'}")

            try:
                data = r.json()
                if isinstance(data, dict):
                    print(f"최상위 키: {list(data.keys())[:12]}")
                elif isinstance(data, list):
                    print(f"리스트 {len(data)}개")
                preview = json.dumps(data, ensure_ascii=False)[:1500]
                print(f"미리보기:\n{preview}")
            except Exception:
                print(f"JSON 아님. 텍스트 앞부분:\n{text[:600]}")
        else:
            print(f"응답 앞부분: {r.text[:150]}")
    except Exception as e:
        print(f"요청 실패: {type(e).__name__}: {e}")

print("\n" + "=" * 70)
print("탐색 완료")
print("=" * 70)
