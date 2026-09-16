"""
네이버 신규 REST API 확인 v4 (임시 진단용)
=========================================
네이버 금융이 Next.js 기반으로 개편되어 HTML에 표가 없음.
→ stock.naver.com 계열 JSON API 후보들을 테스트해서
  종목별 외국인/기관 수급 데이터를 어디서 가져올 수 있는지 확인합니다.
"""

import json
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://stock.naver.com/",
}

TICKER = "005930"

candidates = [
    ("A. 종목 기본정보 (basic)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/basic"),

    ("B. 종목 통합정보 (integration)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/integration"),

    ("C. 종목별 투자자 동향 (trend - 핵심 후보)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/trend"),

    ("D. 종목별 외국인/기관 (frgnLendTrend)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/frgnLendTrend"),

    ("E. stock.naver 도메인 trend",
     f"https://stock.naver.com/api/domestic/stock/{TICKER}/trend"),

    ("F. 일별 시세 (price)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/price?pageSize=10&page=1"),

    ("G. 투자지표 (investmentinfo)",
     f"https://m.stock.naver.com/api/stock/{TICKER}/investmentInfo"),

    ("H. 시장 전체 투자자 동향 (market trend)",
     "https://stock.naver.com/api/domestic/market/trend/daily?marketType=KOSPI"),
]

print("=" * 70)
print("네이버 신규 API 후보 테스트")
print("=" * 70)

for label, url in candidates:
    print(f"\n{'─' * 70}")
    print(f"{label}")
    print(f"URL: {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        print(f"상태코드: {r.status_code}, 길이: {len(r.text)}")

        if r.status_code == 200 and r.text.strip():
            try:
                data = r.json()
                # 구조 요약 출력
                if isinstance(data, dict):
                    print(f"최상위 키: {list(data.keys())[:15]}")
                elif isinstance(data, list):
                    print(f"리스트 {len(data)}개, 첫 항목 키: "
                          f"{list(data[0].keys())[:15] if data and isinstance(data[0], dict) else '?'}")
                # 앞부분 원문
                preview = json.dumps(data, ensure_ascii=False)[:1200]
                print(f"내용 미리보기:\n{preview}")
            except Exception:
                print(f"JSON 아님. 텍스트 앞부분:\n{r.text[:400]}")
        else:
            print(f"응답 앞부분: {r.text[:200]}")
    except Exception as e:
        print(f"요청 실패: {type(e).__name__}: {e}")

print("\n" + "=" * 70)
print("테스트 완료")
print("=" * 70)
