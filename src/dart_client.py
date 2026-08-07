# ============================================================
# DART (전자공시시스템) API 클라이언트
# - 종목코드 <-> DART 고유번호(corp_code) 매핑
# - 분기/연간 재무제표 조회
# - 최근 공시 목록 조회 (신규 분기보고서 감지용)
# ============================================================
import os
import io
import zipfile
import xml.etree.ElementTree as ET
import requests

DART_API_KEY = os.environ.get("DART_API_KEY")
CORP_CODE_CACHE = "data/corp_codes.xml"

QUARTERLY_REPORT_KEYWORDS = ["분기보고서", "반기보고서", "사업보고서"]
PRELIM_EARNINGS_KEYWORDS = ["잠정실적", "손익구조", "영업(잠정)실적", "매출액또는손익구조"]


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


def get_all_listed_corps() -> list:
    """corp_code 매핑 파일에서 실제 상장된(종목코드가 있는) 회사만 리스트로 반환한다."""
    xml_bytes = download_corp_code_map()
    root = ET.fromstring(xml_bytes)

    result = []
    for item in root.findall("list"):
        stock_code = item.findtext("stock_code", "").strip()
        if stock_code:
            result.append({
                "ticker": stock_code,
                "name": item.findtext("corp_name", "").strip(),
                "corp_code": item.findtext("corp_code"),
            })
    return result


def _search_disclosures(corp_code: str, bgn_de: str, end_de: str, pblntf_ty: str, pblntf_detail_ty: str = None) -> list:
    url = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "pblntf_ty": pblntf_ty,
        "page_count": 20,
    }
    if pblntf_detail_ty:
        params["pblntf_detail_ty"] = pblntf_detail_ty

    res = requests.get(url, params=params, timeout=30).json()

    if res.get("status") == "013":
        return []  # 해당 기간 공시 없음
    if res.get("status") != "000":
        raise RuntimeError(f"DART API 오류: {res.get('status')} {res.get('message')}")

    return res.get("list", [])


def get_recent_disclosures(corp_code: str, bgn_de: str, end_de: str) -> list:
    """
    특정 기업의 최근 공시 중, 우리가 알림을 보낼 만한 두 종류를 찾아 합쳐서 반환한다.
    - 정식 정기공시: 분기보고서 / 반기보고서 / 사업보고서 (kind="periodic")
    - 잠정실적 공정공시: 정식 보고서보다 먼저 나오는 실적 예고 성격 (kind="prelim")

    각 항목에 "kind" 키를 추가해서 반환하므로, 호출하는 쪽에서 이 값으로
    분석 방식을 다르게 처리하면 된다.
    """
    results = []

    # 1) 정기공시(사업/반기/분기보고서)
    periodic_items = _search_disclosures(corp_code, bgn_de, end_de, pblntf_ty="A")
    for item in periodic_items:
        if any(keyword in item.get("report_nm", "") for keyword in QUARTERLY_REPORT_KEYWORDS):
            item["kind"] = "periodic"
            results.append(item)

    # 2) 공정공시 중 잠정실적 성격의 공시
    fair_items = _search_disclosures(corp_code, bgn_de, end_de, pblntf_ty="I", pblntf_detail_ty="I001")
    for item in fair_items:
        if any(keyword in item.get("report_nm", "") for keyword in PRELIM_EARNINGS_KEYWORDS):
            item["kind"] = "prelim"
            results.append(item)

    return results


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
