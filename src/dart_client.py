# ============================================================
# DART (전자공시시스템) API 클라이언트
# - 종목코드 <-> DART 고유번호(corp_code) 매핑
# - 분기/연간 재무제표 조회
# ============================================================
import os
import io
import zipfile
import xml.etree.ElementTree as ET
import requests

DART_API_KEY = os.environ.get("DART_API_KEY")
CORP_CODE_CACHE = "data/corp_codes.xml"


def download_corp_code_map():
    """
    DART가 제공하는 전체 상장사 고유번호(corp_code) 매핑 파일을 받아온다.
    파일이 크지 않고 자주 안 바뀌므로 data/corp_codes.xml 로 캐싱해둔다.
    """
    if os.path.exists(CORP_CODE_CACHE):
        with open(CORP_CODE_CACHE, "rb") as f:
            return f.read()

    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    res = requests.get(url, params={"crtfc_key": DART_API_KEY}, timeout=30)
    res.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        xml_bytes = z.read("CORPCODE.xml")

    os.makedirs(os.path.dirname(CORP_CODE_CACHE), exist_ok=True)
    with open(CORP_CODE_CACHE, "wb") as f:
        f.write(xml_bytes)

    return xml_bytes


def get_corp_code(ticker: str) -> str | None:
    """종목코드(6자리)로 DART corp_code(8자리)를 찾는다."""
    xml_bytes = download_corp_code_map()
    root = ET.fromstring(xml_bytes)

    for item in root.findall("list"):
        stock_code = item.findtext("stock_code", "").strip()
        if stock_code == ticker:
            return item.findtext("corp_code")
    return None


def get_financial_statement(corp_code: str, year: str, report_code: str, fs_div: str = "CFS"):
    """
    특정 기업의 재무제표(주요계정 전체)를 가져온다.
    fs_div: CFS(연결) / OFS(별도) - 연결 재무제표가 없는 회사는 OFS로 재시도 필요
    """
    url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bsns_year": year,
        "reprt_code": report_code,
        "fs_div": fs_div,
    }
    res = requests.get(url, params=params, timeout=30).json()

    if res.get("status") == "013":
        # 데이터 없음 (아직 공시 전이거나 해당 분기 보고서 없음)
        return []
    if res.get("status") != "000":
        raise RuntimeError(f"DART API 오류: {res.get('status')} {res.get('message')}")

    return res.get("list", [])


def extract_key_accounts(statement_list: list) -> dict:
    """
    fnlttSinglAcntAll 응답에서 자주 쓰는 핵심 계정만 뽑아서 정리한다.
    (매출액, 영업이익, 당기순이익, 자산총계, 부채총계, 자본총계)
    """
    targets = {
        "매출액": None,
        "영업이익": None,
        "당기순이익": None,
        "자산총계": None,
        "부채총계": None,
        "자본총계": None,
    }

    for row in statement_list:
        name = row.get("account_nm", "").strip()
        if name in targets and targets[name] is None:
            try:
                targets[name] = int(row.get("thstrm_amount", "0").replace(",", ""))
            except (ValueError, AttributeError):
                targets[name] = None

    return targets
