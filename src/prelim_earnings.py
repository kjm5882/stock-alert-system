# ============================================================
# 잠정실적(공정공시) 원문 파싱
#
# 주의: 이 파싱은 "최선을 다해 추정"하는 수준이다.
# 잠정실적 공시는 회사마다 표 양식이 조금씩 달라서 100% 정확하지 않을 수 있다.
# 그래서 항상 원문 링크를 함께 보내서, 사람이 직접 확인할 수 있게 한다.
# 단위(억원/백만원 등)를 못 찾으면 금액 기반 계산(연환산 PER 등)은 생략한다.
# ============================================================
import os
import io
import re
import zipfile
import requests

DART_API_KEY = os.environ.get("DART_API_KEY")

UNIT_MULTIPLIERS = {
    "억원": 100_000_000,
    "백만원": 1_000_000,
    "천원": 1_000,
    "원": 1,
}


def build_viewer_url(rcept_no: str) -> str:
    """사람이 클릭해서 볼 수 있는 DART 공시 원문 페이지 링크."""
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def fetch_document_text(rcept_no: str) -> str:
    """
    공시 원문 문서(XML)를 받아와서 태그를 제거한 순수 텍스트로 반환한다.
    실패하면 빈 문자열을 반환한다 (호출부에서 링크만 보내는 폴백으로 처리).
    """
    try:
        url = "https://opendart.fss.or.kr/api/document.xml"
        res = requests.get(url, params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no}, timeout=30)
        res.raise_for_status()

        text_parts = []
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            for name in z.namelist():
                raw = z.read(name)
                try:
                    decoded = raw.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = raw.decode("euc-kr", errors="ignore")
                text_parts.append(decoded)

        full_text = "\n".join(text_parts)
        clean = re.sub(r"<[^>]+>", " ", full_text)
        clean = re.sub(r"&nbsp;?", " ", clean)
        clean = re.sub(r"[ \t]+", " ", clean)
        return clean

    except Exception:
        return ""


NUMBER_PATTERN = r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?"


def _find_first_number_after(text: str, label: str, window: int = 200):
    """라벨(예: '매출액')이 나온 직후 window자 이내에서 첫 번째 숫자를 찾는다."""
    idx = text.find(label)
    if idx == -1:
        return None
    snippet = text[idx: idx + window]
    match = re.search(NUMBER_PATTERN, snippet)
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def _find_net_income(text: str):
    """
    당기순이익은 '지배주주' 라벨이 붙은 줄을 우선 찾는다 (자회사 소수지분 제외).
    없으면 일반 '당기순이익'으로 폴백한다.
    """
    for label in ["지배주주순이익", "지배기업소유주지분", "지배주주 당기순이익", "지배주주지분"]:
        v = _find_first_number_after(text, label)
        if v is not None:
            return v
    return _find_first_number_after(text, "당기순이익")


def _find_unit(text: str):
    """
    문서 안에서 '(단위 : 억원)' 같은 단위 표기를 찾는다.
    못 찾으면 None을 반환하고, 호출부는 금액 기반 계산을 생략해야 한다.
    """
    match = re.search(r"단위\s*[:：]?\s*(억원|백만원|천원|원)", text)
    if match:
        return match.group(1)
    return None


def parse_prelim_earnings(text: str) -> dict:
    """
    잠정실적 공시 텍스트에서 매출액/영업이익/당기순이익과 단위를 최선을 다해 추정한다.
    반환값의 금액은 "원문에 적힌 그대로의 숫자"이며, 원(KRW) 단위로 변환하려면
    to_won()에 unit과 함께 넘겨야 한다.
    """
    if not text:
        return {"매출액": None, "영업이익": None, "당기순이익": None, "unit": None, "parsed": False}

    revenue = _find_first_number_after(text, "매출액")
    op_income = _find_first_number_after(text, "영업이익")
    net_income = _find_net_income(text)
    unit = _find_unit(text)

    parsed = any(v is not None for v in [revenue, op_income, net_income])

    return {
        "매출액": revenue,
        "영업이익": op_income,
        "당기순이익": net_income,
        "unit": unit,
        "parsed": parsed,
    }


def to_won(amount: float, unit: str):
    """원문 단위(예: '억원')의 금액을 원(KRW) 단위로 변환한다. 단위를 모르면 None."""
    if amount is None or unit not in UNIT_MULTIPLIERS:
        return None
    return amount * UNIT_MULTIPLIERS[unit]
