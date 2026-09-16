"""
네이버 응답 본문 확인 v3 (임시 진단용)
====================================
200 OK인데 iframe도 종목코드도 0개 → 실제로 뭐가 오는지 본문을 직접 봅니다.
인코딩 문제인지, 봇 차단 페이지인지 구분합니다.
"""

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": "https://finance.naver.com/",
}

url = "https://finance.naver.com/item/frgn.naver?code=005930"

print("=" * 60)
print("네이버 응답 본문 직접 확인")
print("=" * 60)

res = requests.get(url, headers=HEADERS, timeout=15)
print(f"\n상태코드: {res.status_code}")
print(f"최종 URL(리다이렉트 후): {res.url}")
print(f"응답 헤더 Content-Type: {res.headers.get('Content-Type')}")
print(f"apparent_encoding: {res.apparent_encoding}")
print(f"현재 encoding: {res.encoding}")
print(f"원본 바이트 길이: {len(res.content)}")

print("\n" + "-" * 60)
print("[A] 원본 바이트 앞부분 (500바이트)")
print("-" * 60)
print(res.content[:500])

print("\n" + "-" * 60)
print("[B] euc-kr 디코딩 앞부분 (1500자)")
print("-" * 60)
try:
    print(res.content.decode("euc-kr", errors="replace")[:1500])
except Exception as e:
    print("euc-kr 디코딩 실패:", e)

print("\n" + "-" * 60)
print("[C] utf-8 디코딩 앞부분 (1500자)")
print("-" * 60)
try:
    print(res.content.decode("utf-8", errors="replace")[:1500])
except Exception as e:
    print("utf-8 디코딩 실패:", e)

print("\n" + "-" * 60)
print("[D] 주요 키워드 존재 여부 (원본 바이트 기준)")
print("-" * 60)
for kw in [b"iframe", b"table", b"005930", b"code=", b"frgn", b"<script"]:
    print(f"  {kw.decode()}: {res.content.count(kw)}회")

print("\n" + "=" * 60)
