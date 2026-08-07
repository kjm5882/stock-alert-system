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
    xml_bytes = download_corp_code_map()
    root = ET.fromstring(xml_bytes)

    for item in root.findall("list"):
        stock_code = item.findtext("stock_code", "").strip()
        if stock_code == ticker:
            return item.findtext("corp_code")
    return None


def get_all_listed_corps() -> list:
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
        return []
    if res.get("status") != "000":
        raise RuntimeError(f"DART API 오류: {res.get('status')} {res.get('message')}")

    return res.get("list", [])


def get_recent_disclosures(corp_code: str, bgn_de: str, end_de: str) -> list:
    results = []

    periodic_items = _search_disclosures(corp_code, bgn_de, end_de, pblntf_ty="A")
    for item in periodic_items:
        if any(keyword in item.get("report_nm", "") for keyword in QUARTERLY_REPORT_KEYWORDS):
            item["kind"] = "periodic"
            results.append(item)

    fair_items = _search_disclosures(corp_code, bgn_de, end_de, pblntf_ty="I", pblntf_detail_ty="I001")
    for item in fair_items:
        if any(keyword in item.get("report_nm", "") for keyword in PRELIM_EARNINGS_KEYWORDS):
            item["kind"] = "prelim"
            results.append(item)

    return results


def get_financial_statement(corp_code: str, year: str, report_code: str, fs_div: str = "CFS"):
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
        return []
    if res.get("status") != "000":
        raise RuntimeError(f"DART API 오류: {res.get('status')} {res.get('message')}")

    return res.get("list", [])


# ------------------------------------------------------------
# 계정 매칭 전략
# 1순위: account_id (IFRS/DART 표준 코드) - 회사와 무관하게 일관됨, 가장 신뢰도 높음
# 2순위: account_nm 텍스트에 키워드가 "포함"되는지 - account_id를 못 찾은 경우의 보조 수단
#
# 표준계정과목이 아닌 회사(일부 특수업종 등)는 account_id가 다르게 잡힐 수 있어서
# 2순위 보조 수단을 남겨두되, 1순위가 있으면 그걸 우선한다.
# ------------------------------------------------------------
ACCOUNT_ID_MAP = {
    "매출액": ["ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"],
    "영업이익": ["dart_OperatingIncomeLoss"],
    "당기순이익": ["ifrs-full_ProfitLoss"],
    "자산총계": ["ifrs-full_Assets"],
    "부채총계": ["ifrs-full_Liabilities"],
    "자본총계": ["ifrs-full_Equity"],
}

ACCOUNT_TEXT_FALLBACK = {
    "매출액": ["매출액"],
    "영업이익": ["영업이익"],
    "당기순이익": ["당기순이익", "반기순이익", "분기순이익"],
    "자산총계": ["자산총계"],
    "부채총계": ["부채총계"],
    "자본총계": ["자본총계"],
}


def extract_key_accounts(statement_list: list) -> dict:
    """
    fnlttSinglAcntAll 응답에서 자주 쓰는 핵심 계정만 뽑아서 정리한다.
    account_id(표준코드)를 우선 사용하고, 없으면 계정명 텍스트로 보조 판단한다.
    """
    targets = {k: None for k in ACCOUNT_ID_MAP}

    # 1순위: account_id 매칭
    for row in statement_list:
        account_id = row.get("account_id", "")
        for target, ids in ACCOUNT_ID_MAP.items():
            if targets[target] is not None:
                continue
            if account_id in ids:
                try:
                    targets[target] = int(row.get("thstrm_amount", "0").replace(",", ""))
                except (ValueError, AttributeError):
                    pass

    # 2순위: account_id로 못 찾은 항목만 텍스트로 보조 검색
    remaining = [t for t, v in targets.items() if v is None]
    if remaining:
        for row in statement_list:
            name = row.get("account_nm", "").strip()
            if not name:
                continue
            for target in remaining:
                if targets[target] is not None:
                    continue
                if any(keyword in name for keyword in ACCOUNT_TEXT_FALLBACK[target]):
                    try:
                        targets[target] = int(row.get("thstrm_amount", "0").replace(",", ""))
                    except (ValueError, AttributeError):
                        pass

    return targets
