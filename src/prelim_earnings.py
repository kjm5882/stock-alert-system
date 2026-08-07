# ============================================================
# 잠정실적(공정공시) 원문 파싱
#
# 주의: 이 파싱은 "최선을 다해 추정"하는 수준이다.
# 잠정실적 공시는 회사마다 표 양식이 조금씩 달라서 100% 정확하지 않을 수 있다.
# 그래서 항상 원문 링크를 함께 보내서, 사람이 직접 확인할 수 있게 한다.
# ============================================================
import os
import io
import re
import zipfile
import requests

DART_API_KEY = os.environ.get("DART_API_KEY")


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
        # 태그 제거 (표 구조는 잃지만, 라벨과 숫자의 순서는 유지됨)
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


def parse_prelim_earnings(text: str) -> dict:
    """
    잠정실적 공시 텍스트에서 매출액/영업이익/당기순이익을 최선을 다해 추정한다.
    단위(백만원/억원 등)는 신뢰도가 낮아 별도 표기하지 않고,
    사람이 원문에서 직접 단위를 확인하도록 안내 문구를 함께 보낸다.
    """
    if not text:
        return {"매출액": None, "영업이익": None, "당기순이익": None, "parsed": False}

    revenue = _find_first_number_after(text, "매출액")
    op_income = _find_first_number_after(text, "영업이익")
    net_income = _find_first_number_after(text, "당기순이익")

    parsed = any(v is not None for v in [revenue, op_income, net_income])

    return {
        "매출액": revenue,
        "영업이익": op_income,
        "당기순이익": net_income,
        "parsed": parsed,
    }
