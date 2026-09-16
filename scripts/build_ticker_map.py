"""
종목명 ↔ 티커 매핑 파일 생성기
=============================
목적: data/ticker_name_map.json 을 만든다.
     수급 담당자가 "삼성전자" 같은 이름을 "005930" 코드로 바꿀 때 사용.

데이터 출처: 이미 저장소에 있는 data/corp_codes.xml (DART 기업코드 파일).
     이 파일에는 DART에 등록된 모든 기업의 이름과 종목코드가 들어있고,
     상장사는 stock_code 항목이 채워져 있다.

corp_codes.xml 이 없거나 오래됐으면 DART API로 새로 받아온다.
(DART_API_KEY 환경변수 필요)

실행 주기: 한 달에 한 번 정도면 충분 (신규 상장/상장폐지 반영).
"""

import os
import io
import json
import zipfile
import xml.etree.ElementTree as ET

import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CORP_CODES_FILE = os.path.join(DATA_DIR, "corp_codes.xml")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_name_map.json")

DART_API_KEY = os.environ.get("DART_API_KEY")


def download_corp_codes():
    """DART에서 기업코드 파일을 새로 받아온다."""
    if not DART_API_KEY:
        print("[오류] DART_API_KEY 환경변수가 없습니다.")
        return False

    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    print("[DART] 기업코드 파일 다운로드 중...")
    try:
        res = requests.get(url, params={"crtfc_key": DART_API_KEY}, timeout=60)
        res.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            xml_name = z.namelist()[0]
            with z.open(xml_name) as f:
                content = f.read()

        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CORP_CODES_FILE, "wb") as f:
            f.write(content)
        print(f"[DART] 다운로드 완료 ({len(content):,} 바이트)")
        return True
    except Exception as e:
        print(f"[DART] 다운로드 실패: {e}")
        return False


def build_ticker_map():
    """corp_codes.xml 을 읽어서 {종목명: 티커} 사전을 만든다."""
    if not os.path.exists(CORP_CODES_FILE):
        print("[알림] corp_codes.xml 이 없습니다. 새로 받아옵니다.")
        if not download_corp_codes():
            return None

    print("[매핑] corp_codes.xml 파싱 중...")
    try:
        tree = ET.parse(CORP_CODES_FILE)
        root = tree.getroot()
    except Exception as e:
        print(f"[매핑] XML 파싱 실패: {e}")
        return None

    name_to_ticker = {}
    total = 0

    for item in root.iter("list"):
        total += 1
        corp_name_el = item.find("corp_name")
        stock_code_el = item.find("stock_code")

        if corp_name_el is None or stock_code_el is None:
            continue

        corp_name = (corp_name_el.text or "").strip()
        stock_code = (stock_code_el.text or "").strip()

        # 상장사만 (비상장은 stock_code가 비어있음)
        if not corp_name or not stock_code or len(stock_code) != 6:
            continue

        name_to_ticker[corp_name] = stock_code

    print(f"[매핑] 전체 {total:,}개 기업 중 상장사 {len(name_to_ticker):,}개 추출")
    return name_to_ticker


def main():
    ticker_map = build_ticker_map()

    if not ticker_map:
        print("[실패] 매핑 파일을 만들지 못했습니다.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TICKER_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(ticker_map, f, ensure_ascii=False, indent=2)

    print(f"[완료] {TICKER_MAP_FILE} 저장 ({len(ticker_map):,}개 종목)")

    # 샘플 확인
    print("\n[확인] 주요 종목 매핑 상태:")
    for name in ["삼성전자", "SK하이닉스", "한국콜마", "코스메카코리아",
                 "실리콘투", "에이피알", "알테오젠", "메지온"]:
        code = ticker_map.get(name)
        print(f"  {name}: {code if code else '❌ 없음'}")


if __name__ == "__main__":
    main()
