# ============================================================
# DART (전자공시시스템) API 클라이언트
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
# 계정 매칭 전략 (우선순위 기반)
#
# 각 항목마다 "우선순위가 높은 account_id부터" 문서 전체에서 찾는다.
# 예: 당기순이익은 "지배주주 귀속분"을 1순위로, 없으면 전체 당기순이익으로 폴백.
# account_id를 하나도 못 찾으면 텍스트 키워드로 최후 보조 검색한다.
# ------------------------------------------------------------
# ------------------------------------------------------------
# 계정 매칭 전략 (우선순위 + 재무제표 구분(sj_div) 제한)
#
# 실제 DART 응답을 직접 확인해보니 아래 두 가지 함정이 있었다:
# 1. account_id가 "손익계산서(IS/CIS)"뿐 아니라 "자본변동표(SCE)"에도
#    지분별로 여러 번 등장해서, 리스트 순서에 따라 엉뚱한 값을 집어올 수 있다.
#    -> 매출/영업이익/순이익은 sj_div가 IS 또는 CIS인 행만 본다.
# 2. "주당순이익(EPS)" 같은 항목이 "분기순이익" 등의 텍스트를 부분 포함하고 있어서
#    텍스트 기반 보조검색에서 잘못 걸릴 수 있다 (예: "기본주당분기순이익").
#    -> "주당"이 들어간 행은 텍스트 매칭에서 아예 제외한다.
# ------------------------------------------------------------
IS_LIKE_DIVISIONS = ("IS", "CIS")
BS_LIKE_DIVISIONS = ("BS",)

ACCOUNT_ID_PRIORITY = {
    "매출액": {"ids": ["ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"], "sj_div": IS_LIKE_DIVISIONS},
    "영업이익": {"ids": ["dart_OperatingIncomeLoss"], "sj_div": IS_LIKE_DIVISIONS},
    "당기순이익": {
        "ids": ["ifrs-full_ProfitLossAttributableToOwnersOfParent", "ifrs-full_ProfitLoss"],
        "sj_div": IS_LIKE_DIVISIONS,
    },
    "자산총계": {"ids": ["ifrs-full_Assets"], "sj_div": BS_LIKE_DIVISIONS},
    "부채총계": {"ids": ["ifrs-full_Liabilities"], "sj_div": BS_LIKE_DIVISIONS},
    "자본총계": {"ids": ["ifrs-full_Equity"], "sj_div": BS_LIKE_DIVISIONS},
}

ACCOUNT_TEXT_FALLBACK = {
    "매출액": {"keywords": ["매출액"], "sj_div": IS_LIKE_DIVISIONS},
    "영업이익": {"keywords": ["영업이익"], "sj_div": IS_LIKE_DIVISIONS},
    "당기순이익": {"keywords": ["지배주주", "당기순이익", "반기순이익", "분기순이익"], "sj_div": IS_LIKE_DIVISIONS},
    "자산총계": {"keywords": ["자산총계"], "sj_div": BS_LIKE_DIVISIONS},
    "부채총계": {"keywords": ["부채총계"], "sj_div": BS_LIKE_DIVISIONS},
    "자본총계": {"keywords": ["자본총계"], "sj_div": BS_LIKE_DIVISIONS},
}


def _parse_amount(row):
    try:
        return int(row.get("thstrm_amount", "0").replace(",", ""))
    except (ValueError, AttributeError):
        return None


def extract_key_accounts(statement_list: list) -> dict:
    """
    fnlttSinglAcntAll 응답에서 핵심 계정을 뽑는다.
    - account_id 우선순위대로 찾되, 반드시 해당 항목에 맞는 재무제표 구분(sj_div) 안에서만 찾는다.
    - 그래도 못 찾으면 텍스트 키워드로 보조 검색하되, "주당"이 포함된 행(EPS 등)은 제외한다.
    """
    targets = {k: None for k in ACCOUNT_ID_PRIORITY}

    for target, cfg in ACCOUNT_ID_PRIORITY.items():
        for account_id in cfg["ids"]:
            found = False
            for row in statement_list:
                if row.get("sj_div") not in cfg["sj_div"]:
                    continue
                if row.get("account_id") == account_id:
                    amount = _parse_amount(row)
                    if amount is not None:
                        targets[target] = amount
                        found = True
                        break
            if found:
                break

    remaining = [t for t, v in targets.items() if v is None]
    if remaining:
        for row in statement_list:
            name = row.get("account_nm", "").strip()
            if not name or "주당" in name:
                continue
            for target in remaining:
                if targets[target] is not None:
                    continue
                cfg = ACCOUNT_TEXT_FALLBACK[target]
                if row.get("sj_div") not in cfg["sj_div"]:
                    continue
                if any(keyword in name for keyword in cfg["keywords"]):
                    amount = _parse_amount(row)
                    if amount is not None:
                        targets[target] = amount

    return targets
