"""
매칭 엔진 (Matching Engine)
==========================
목적: 세 담당자의 신호를 교차해서 "골든존" 종목을 찾아낸다.

  📰 내러티브 (피드 담당자)  : 요즘 블로거/유튜버가 많이, 긍정적으로 얘기하는가
  📊 펀더멘탈 (이 스크립트)  : 그 종목의 현재 지표가 자기 과거 대비 어디쯤인가
  💹 수급     (수급 담당자)  : 외국인/기관이 실제로 사고 있는가

데이터 출처 (KRX 차단 이슈로 pykrx 미사용):
  - DART 연간 재무    : 매출액/영업이익/당기순이익/자본총계 (최대 12년)
  - 네이버 연간 재무표 : 연도별 PER (보통 3~5년) — 네이버가 이미 계산해둔 값을 그대로 사용
  - 네이버 integration: 현재 주가, 현재 PER(TTM)

지표별 계산 방식:
  - PER        : 네이버 연도별 PER과 현재 PER(TTM)을 비교
                 * 직접 계산하려면 발행주식수와 장기 주가가 필요한데, 그 과정에서
                   증자·액면분할 보정 오차가 생긴다. 네이버 값은 당시 실제 주식수로
                   계산된 값이라 더 정확하고 수집 단계도 단순하다.
  - 영업이익률  : 영업이익 / 매출액        (DART)
  - ROE        : 당기순이익 / 자본총계     (DART)
  - 매출성장률  : 전년 대비 매출 증감률    (DART)

⚠️ 알려진 한계 (결과에 함께 표기됨)
  - PER 이력이 3~5년뿐이라 '과거 대비' 판단의 표본이 짧다. 사이클이 긴 업종은
    한 사이클을 겨우 담는 수준이므로 백분위를 과신하지 말 것.
  - 현재 PER은 최근 4개 분기(TTM) 기준, 과거 PER은 각 연도 기준이라 시점 정의가 다르다.
  - 자본총계는 연결 기준(비지배지분 포함)이라 ROE가 약간 낮게 나올 수 있다.
  - 수익성 이력이 3년 미만이고 PER 이력도 없으면 '이력부족'으로 판단에서 제외한다.
"""

import os
import re
import json
import time
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

# GitHub Actions는 UTC로 돌아가므로, 사람이 읽는 시각은 한국시간으로 바꿔서 표시한다.
KST = timezone(timedelta(hours=9))

# ── 설정 ──────────────────────────────────────────────
LOOKBACK_DAYS_FOR_MENTIONS = 14   # 피드에서 며칠치 언급을 볼지
MIN_HISTORY_YEARS = 3             # 이력이 이보다 적으면 판단 제외
DEBUG_FIRST_N = 2                 # 처음 N개 종목은 원본 응답까지 상세 출력 (문제 추적용)

# PER 유효 범위 — 이 밖의 값은 밸류에이션 지표로서 의미가 없어 제외한다.
#   0 이하 : 적자 연도 (PER 자체가 성립 안 함)
#   100 초과: 이익이 시총 대비 1% 미만 → '비싸다'가 아니라 '이익이 없다'는 뜻
PER_VALID_MIN = 0.1
PER_VALID_MAX = 100.0
MIN_VALID_PER_SAMPLES = 2   # 유효 과거 PER이 이보다 적으면 밸류 판정 보류

# ── 펀더멘탈 점수 배점 (합계 100) ──
# 지표마다 성격이 달라 절대 기준과 상대 기준을 섞는다.
#   절대가 맞는 것 : 부채비율·매출성장률·시가배당률
#       업종과 무관하게 통용되는 좋고 나쁨의 기준이 있다.
#   상대가 맞는 것 : 영업이익률
#       조선 5%와 바이오 45%를 같은 자로 잴 수 없다. 자기 과거와 비교해야 한다.
#   PER : 업종 평균과 비교(가능하면). 자기 과거는 표본이 3년뿐이라 해상도가 낮고,
#         절대 기준은 업종 편향을 만든다.
FUNDA_WEIGHTS = {
    "PER": 25,
    "영업이익률": 25,
    "부채비율": 20,
    "매출성장률": 15,
    "시가배당률": 15,
}
FUNDA_GOOD_THRESHOLD = 60      # 펀더멘탈 점수가 이 이상이면 '양호'로 본다

# 골든존 기준
NARRATIVE_MIN_SOURCES = 2         # 서로 다른 블로거/채널 최소 몇 곳에서 언급돼야 하는지
NARRATIVE_MIN_SCORE = 50          # 내러티브 점수 하한
VALUATION_MAX_PERCENTILE = 40     # (구버전 호환) PER 자기과거 백분위 기준

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FEED_SIGNALS_FILE = os.path.join(DATA_DIR, "feed_signals.jsonl")
SUPPLY_SIGNALS_FILE = os.path.join(DATA_DIR, "supply_demand_signals.jsonl")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_name_map.json")
CORP_CODES_FILE = os.path.join(DATA_DIR, "corp_codes.xml")
FUNDA_CACHE_FILE = os.path.join(DATA_DIR, "fundamental_cache.json")
RESULTS_FILE = os.path.join(DATA_DIR, "matching_results.json")
HISTORY_FILE = os.path.join(DATA_DIR, "matching_history.jsonl")
OWNERSHIP_FILE = os.path.join(DATA_DIR, "ownership.json")

DART_API_KEY = os.environ.get("DART_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}

# 블로그/유튜브에서 흔히 쓰는 약칭 → 정식 종목명
ALIAS_MAP = {
    "콜마": "한국콜마",
    "코스메카": "코스메카코리아",
    "SKT": "SK텔레콤",
    "네이버": "NAVER",
    "한조": "HD한국조선해양",
    "한국조선해양": "HD한국조선해양",
    "현대중공업": "HD현대중공업",
    "현대일렉트릭": "HD현대일렉트릭",
    "하이닉스": "SK하이닉스",
}

# 지주사/계열사 구분이 모호해 자동 매칭하지 않는 이름
AMBIGUOUS_NAMES = {"LG", "한화", "현대", "SK", "롯데", "CJ", "GS"}


# ── 공통 유틸 ─────────────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def to_number(text):
    """'1,234' / '-1,234' / '-' / None → 숫자 또는 None"""
    if text is None:
        return None
    s = str(text).replace(",", "").replace("+", "").strip()
    if s in ("", "-", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_valid_per(value):
    """PER이 밸류에이션 지표로서 의미 있는 범위인지."""
    return value is not None and PER_VALID_MIN <= value <= PER_VALID_MAX


# 부채비율이 이 값을 넘으면 절대 기준을 적용하지 않는다.
# 금융업(증권·은행·보험)은 구조적으로 수백~수천%가 정상이라 같은 자로 잴 수 없다.
# 업종 코드로 자동 판별할 수 없으므로, 점수에서 제외하고 사람이 확인하도록 남긴다.
DEBT_RATIO_UNSCORABLE = 400.0


def score_debt_ratio(v):
    """부채비율 — 낮을수록 좋다. 0~100점으로 환산.

    400%를 넘으면 점수를 매기지 않고 None을 돌려준다.
    금융업일 가능성이 높은데, 부실기업도 높게 나올 수 있어 자동으로 구분할 수 없다.
    잘못된 점수를 주는 것보다 해당 축을 빼고 나머지로 환산하는 편이 안전하다.
    """
    if v is None:
        return None
    if v > DEBT_RATIO_UNSCORABLE:
        return None
    if v <= 50:
        return 100
    if v <= 100:
        return 80
    if v <= 150:
        return 60
    if v <= 200:
        return 35
    return 10


def score_revenue_growth(v):
    """매출성장률 — 높을수록 좋다. 업종과 무관하게 통용된다."""
    if v is None:
        return None
    if v >= 30:
        return 100
    if v >= 15:
        return 80
    if v >= 5:
        return 60
    if v >= 0:
        return 35
    return 10


def score_dividend_yield(v):
    """시가배당률 — 높을수록 좋다. 무배당은 0점.

    성장주에 불리한 지표라는 점은 감안하고 배점을 15점으로 제한했다.
    """
    if v is None:
        return None
    if v >= 5:
        return 100
    if v >= 3:
        return 80
    if v >= 1.5:
        return 55
    if v > 0:
        return 30
    return 0


def score_per(current_per, industry_per, past_pers):
    """PER 점수. 업종 평균이 있으면 그것과 비교하고, 없으면 자기 과거로 대체한다.

    반환: (0~100점, 근거 문구)
    """
    if not is_valid_per(current_per):
        return None, "PER 무효(적자·이익 미미)"

    if industry_per and industry_per > 0:
        discount = (industry_per - current_per) / industry_per * 100
        if discount >= 30:
            s = 100
        elif discount >= 10:
            s = 80
        elif discount >= -10:
            s = 55
        elif discount >= -30:
            s = 30
        else:
            s = 10
        return s, f"동종업계 {industry_per:.1f} 대비 {discount:+.0f}%"

    if len(past_pers) >= MIN_VALID_PER_SAMPLES:
        pct = percentile_rank(current_per, past_pers)
        return round(100 - pct, 1), f"자기 과거 {len(past_pers)}년 중 백분위 {pct}"

    return None, "비교 기준 없음"


def combine_funda_score(parts):
    """확보된 지표만으로 100점 만점 환산.

    일부 지표가 없는 종목도 나머지로 공정하게 비교할 수 있도록,
    확보된 지표의 배점 합을 분모로 삼는다.
    """
    got = {k: v for k, v in parts.items() if v is not None}
    if not got:
        return None, 0
    total_w = sum(FUNDA_WEIGHTS[k] for k in got)
    score = sum(got[k] * FUNDA_WEIGHTS[k] for k in got) / total_w
    return round(score, 1), total_w


def percentile_rank(value, history):
    """value가 history 분포에서 몇 번째 백분위인지 (0~100). 낮을수록 분포 하단."""
    if value is None or not history:
        return None
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return round((below + 0.5 * equal) / len(history) * 100, 1)


# ── 1) 내러티브 수집과 점수화 ─────────────────────────
def collect_narrative_raw():
    """feed_signals.jsonl → 종목명별 원시 집계 (점수는 아직 안 냄).

    점수를 바로 내지 않는 이유: 같은 종목이 '한조'/'HD한국조선해양'처럼
    다른 이름으로 언급될 수 있어서, 티커로 합친 뒤에 점수를 내야
    출처 수가 쪼개지지 않는다.
    """
    if not os.path.exists(FEED_SIGNALS_FILE):
        print("[알림] feed_signals.jsonl 이 없습니다. 피드 담당자를 먼저 실행하세요.")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS_FOR_MENTIONS)
    per_name = {}

    with open(FEED_SIGNALS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["timestamp"])
            except Exception:
                continue
            if ts < cutoff:
                continue
            name = (rec.get("종목명") or "").strip()
            if not name:
                continue

            entry = per_name.setdefault(name, {
                "언급건수": 0, "출처": set(), "긍정": 0, "부정": 0, "중립": 0,
                "확신도높음": 0, "근거": [], "언급된이름": set(),
            })
            entry["언급건수"] += 1
            entry["출처"].add(rec.get("source_name", "?"))
            entry["언급된이름"].add(name)
            tone = rec.get("논조", "중립")
            entry[tone if tone in ("긍정", "부정", "중립") else "중립"] += 1
            if rec.get("확신도") == "높음":
                entry["확신도높음"] += 1
            reason = rec.get("언급이유")
            if reason and len(entry["근거"]) < 5:
                entry["근거"].append(f"[{rec.get('source_name')}] {reason}")

    return per_name


def merge_raw(a, b):
    """같은 티커로 판명된 두 집계를 하나로 합친다."""
    merged = {
        "언급건수": a["언급건수"] + b["언급건수"],
        "출처": a["출처"] | b["출처"],
        "언급된이름": a["언급된이름"] | b["언급된이름"],
        "긍정": a["긍정"] + b["긍정"],
        "부정": a["부정"] + b["부정"],
        "중립": a["중립"] + b["중립"],
        "확신도높음": a["확신도높음"] + b["확신도높음"],
        "근거": (a["근거"] + b["근거"])[:5],
    }
    return merged


def score_narrative(e):
    """원시 집계 → 내러티브 점수.

    점수 구성 (최대 100점):
      - 서로 다른 출처(블로그/채널) 수 : 1곳 10점 → 2곳 28점 → 3곳 46점 → 4곳 이상 60점
      - 긍정 논조 비율                : 최대 25점
      - 확신도 '높음' 비율            : 최대 15점

    출처 1곳짜리 언급은 의도적으로 낮게 잡는다. 한 블로거가 한 번 언급한 것은
    '내러티브가 형성됐다'고 보기 어렵고, 여러 사람이 동시에 얘기하는 것이 신호이기 때문.
    """
    n = e["언급건수"]
    sources = len(e["출처"])
    source_score = min(60, 10 + (sources - 1) * 18)
    positive_score = (e["긍정"] / n) * 25 if n else 0
    confidence_score = (e["확신도높음"] / n) * 15 if n else 0

    return {
        "내러티브점수": round(source_score + positive_score + confidence_score, 1),
        "언급건수": n,
        "출처수": sources,
        "출처목록": sorted(e["출처"]),
        "언급된이름": sorted(e["언급된이름"]),
        "논조": {"긍정": e["긍정"], "중립": e["중립"], "부정": e["부정"]},
        "근거": e["근거"],
    }


# ── 2) 수급 신호 ──────────────────────────────────────
def load_supply_signals():
    """supply_demand_signals.jsonl → 종목명별 최신 수급 신호."""
    if not os.path.exists(SUPPLY_SIGNALS_FILE):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS_FOR_MENTIONS)
    latest = {}
    with open(SUPPLY_SIGNALS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["timestamp"])
            except Exception:
                continue
            if ts < cutoff:
                continue
            name = rec.get("종목명")
            if name:
                latest[name] = rec  # 나중 줄이 더 최신이므로 덮어씀
    return latest


# ── 3) 종목명 → 티커 / 기업코드 ───────────────────────
def build_ticker_to_corpcode():
    """corp_codes.xml → {티커: 기업코드} (DART 조회용)."""
    if not os.path.exists(CORP_CODES_FILE):
        print("[오류] corp_codes.xml 이 없습니다.")
        return {}
    try:
        root = ET.parse(CORP_CODES_FILE).getroot()
    except Exception as e:
        print(f"[오류] corp_codes.xml 파싱 실패: {e}")
        return {}

    mapping = {}
    for item in root.iter("list"):
        corp_code_el = item.find("corp_code")
        stock_code_el = item.find("stock_code")
        if corp_code_el is None or stock_code_el is None:
            continue
        stock_code = (stock_code_el.text or "").strip()
        corp_code = (corp_code_el.text or "").strip()
        if len(stock_code) == 6 and corp_code:
            mapping[stock_code] = corp_code
    return mapping


def match_name_to_ticker(name, ticker_map):
    if name in AMBIGUOUS_NAMES:
        return None
    if name in ticker_map:
        return ticker_map[name]
    if name in ALIAS_MAP and ALIAS_MAP[name] in ticker_map:
        return ticker_map[ALIAS_MAP[name]]
    normalized = name.replace("(주)", "").replace("주식회사", "").strip()
    return ticker_map.get(normalized)


# ── 4) DART 연간 재무 이력 ────────────────────────────
DART_ACCOUNTS = {"매출액", "영업이익", "당기순이익", "자본총계", "부채총계"}


def fetch_dart_annual_block(corp_code, bsns_year):
    """DART 주요계정 1회 호출로 해당연도 + 직전 2개년 데이터를 한꺼번에 받는다.
    반환: {연도(int): {계정명: 금액}}
    """
    url = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bsns_year": str(bsns_year),
        "reprt_code": "11011",  # 사업보고서(연간)
    }
    try:
        res = requests.get(url, params=params, timeout=30)
        data = res.json()
    except Exception as e:
        print(f"    [DART 오류] {corp_code} {bsns_year}: {e}")
        return {}

    if data.get("status") != "000":
        return {}

    rows = data.get("list", [])
    # 연결(CFS) 우선, 없으면 별도(OFS)
    fs_divs = {r.get("fs_div") for r in rows}
    use_div = "CFS" if "CFS" in fs_divs else ("OFS" if "OFS" in fs_divs else None)
    if not use_div:
        return {}

    result = {}
    for r in rows:
        if r.get("fs_div") != use_div:
            continue
        account = (r.get("account_nm") or "").strip()
        if account not in DART_ACCOUNTS:
            continue
        for year_offset, amount_key in [
            (0, "thstrm_amount"), (1, "frmtrm_amount"), (2, "bfefrmtrm_amount")
        ]:
            year = bsns_year - year_offset
            value = to_number(r.get(amount_key))
            if value is not None:
                result.setdefault(year, {})[account] = value
    return result


def fetch_dart_history(corp_code):
    """최대 12년치 연간 재무를 모은다 (4번 호출로 커버)."""
    current_year = datetime.now().year
    # 사업보고서는 보통 이듬해 3월 공시 → 아직 안 나왔을 수 있으니 작년부터 시작
    anchor_years = [current_year - 1, current_year - 4,
                    current_year - 7, current_year - 10]

    merged = {}
    for year in anchor_years:
        block = fetch_dart_annual_block(corp_code, year)
        for y, accounts in block.items():
            merged.setdefault(y, {}).update(accounts)
        time.sleep(0.15)
    return merged


# 발행주식수가 들어있을 수 있는 DART 필드명 후보 (우선순위 순)
def parse_korean_amount(text):
    """'1,461조 5,697억' → 146156970000000.0 (원 단위)"""
    if not text:
        return None
    s = str(text).replace(",", "").replace(" ", "")
    total = 0.0
    matched = False
    for unit, mult in [("조", 1e12), ("억", 1e8), ("만", 1e4)]:
        m = re.search(rf"(\d+(?:\.\d+)?){unit}", s)
        if m:
            total += float(m.group(1)) * mult
            matched = True
            s = s.replace(m.group(0), "")
    leftover = re.sub(r"[^\d.]", "", s)
    if leftover:
        try:
            total += float(leftover)
            matched = True
        except ValueError:
            pass
    return total if matched and total > 0 else None


# ── 5) 네이버 연도별 지표 (PER 담당) ──────────────────
# 네이버가 이미 계산해둔 연도별 PER을 그대로 쓴다.
# 직접 계산(주가 ÷ EPS)하려면 발행주식수와 10년치 주가가 필요한데,
# 그 과정에서 증자·액면분할 보정 오차가 생기고 수집 실패 지점도 늘어난다.
# 네이버 값은 당시 실제 주식수로 계산된 값이라 더 정확하고 수집도 단순하다.
# 대신 제공 연수가 3~5년으로 짧아, 긴 이력이 필요한 수익성 지표는 DART를 쓴다.

# 네이버 연간 재무표의 행 제목은 'PER(배)', 'ROE(지배주주)'처럼 접미사가 붙는다.
NAVER_ROW_PATTERNS = {
    "PER": ["PER"],
    "영업이익률": ["영업이익률"],
    "ROE": ["ROE"],
    "부채비율": ["부채비율"],
    "주당배당금": ["주당배당금"],
    "매출액": ["매출액"],
}


def fetch_naver_annual_metrics(ticker, debug=False):
    """네이버 연간 재무 API → {지표명: {연도: 값}}.

    컨센서스(추정치) 열은 제외하고 실적 확정 연도만 담는다.
    """
    url = f"https://m.stock.naver.com/api/stock/{ticker}/finance/annual"
    result = {k: {} for k in NAVER_ROW_PATTERNS}

    try:
        res = requests.get(url, headers=NAVER_HEADERS, timeout=25)
        if debug:
            print(f"    [진단] 연간지표 status={res.status_code}")
        if res.status_code != 200:
            print(f"    [연간지표 실패] {ticker}: 상태코드 {res.status_code}")
            return result
        data = res.json()
    except Exception as e:
        print(f"    [연간지표 오류] {ticker}: {e}")
        return result

    finance_info = data.get("financeInfo") or {}
    titles = finance_info.get("trTitleList") or []
    rows = finance_info.get("rowList") or []

    # 컨센서스가 아닌(실적 확정) 열만 사용
    actual_keys = {}
    for t in titles:
        if str(t.get("isConsensus", "N")).upper() == "Y":
            continue
        key = str(t.get("key", ""))
        if len(key) >= 4 and key[:4].isdigit():
            actual_keys[key] = int(key[:4])

    if debug:
        print(f"    [진단] 실적 확정 연도: {sorted(actual_keys.values())}")
        print(f"    [진단] 행 제목: {[r.get('title') for r in rows]}")

    for row in rows:
        title = (row.get("title") or "").strip()
        for metric, patterns in NAVER_ROW_PATTERNS.items():
            if not any(pat in title for pat in patterns):
                continue
            # '영업이익률'과 '영업이익'을 혼동하지 않도록 정확도 확인
            if metric == "매출액" and "매출액" not in title:
                continue
            for key, year in actual_keys.items():
                value = to_number((row.get("columns", {}).get(key) or {}).get("value"))
                if value is not None:
                    result[metric][year] = value
            break

    if debug:
        print(f"    [진단] 네이버 PER 이력: {result['PER']}")
    return result


# 동종업계 PER을 구할 때 같은 종목을 여러 번 조회하지 않도록 실행 중에만 기억한다.
# (여러 종목이 같은 경쟁사를 공유하므로 호출 수가 크게 줄어든다)
_PEER_PER_CACHE = {}
PEER_MAX = 6              # 경쟁사 조회 상한 (응답에 보통 4~5개가 온다)
PEER_MIN_VALID = 3        # 중앙값을 쓰려면 최소 이만큼은 유효 PER이 있어야 한다


def _per_from_integration(data):
    """integration 응답에서 PER(TTM) 하나만 꺼낸다."""
    for row in data.get("totalInfos", []):
        if row.get("code") == "per":
            return to_number(re.sub(r"[^\d.\-]", "", str(row.get("value"))))
    return None


def fetch_peer_per(code):
    """경쟁사 한 종목의 PER. 실패하면 None (캐시에도 None으로 남겨 재시도하지 않는다)."""
    if code in _PEER_PER_CACHE:
        return _PEER_PER_CACHE[code]
    per = None
    try:
        res = requests.get(f"https://m.stock.naver.com/api/stock/{code}/integration",
                           headers=NAVER_HEADERS, timeout=15)
        if res.status_code == 200:
            per = _per_from_integration(res.json())
    except Exception:
        per = None
    if not is_valid_per(per):
        per = None
    _PEER_PER_CACHE[code] = per
    return per


def industry_per_from_peers(peer_codes, debug=False):
    """동일업종 비교군의 PER 중앙값.

    네이버 integration의 industryCompareInfo는 '업종 평균'이 아니라
    경쟁사 목록(리스트)으로 오고, 각 항목에는 주가·시총만 있고 PER이 없다.
    그래서 경쟁사별로 PER을 따로 받아 중앙값을 낸다.
    평균이 아니라 중앙값인 이유: 한 종목의 비정상 PER이 기준을 흔들지 않게 하려고.
    """
    pers = []
    for code in peer_codes[:PEER_MAX]:
        p = fetch_peer_per(code)
        if p is not None:
            pers.append(p)
        time.sleep(0.15)
    if len(pers) < PEER_MIN_VALID:
        if debug:
            print(f"    [진단] 동종업계 유효 PER {len(pers)}개 → 기준 미달, 자기 과거로 대체")
        return None, len(pers)
    pers.sort()
    n = len(pers)
    med = pers[n // 2] if n % 2 else (pers[n // 2 - 1] + pers[n // 2]) / 2
    if debug:
        print(f"    [진단] 동종업계 PER {pers} → 중앙값 {med:.1f}")
    return round(med, 2), len(pers)


def fetch_naver_current(ticker, debug=False):
    """네이버 integration → 현재가, 시가총액, 네이버 자체 PER(TTM), 동종업계 PER."""
    url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
    info = {"현재가": None, "시가총액": None, "네이버PER": None,
            "추정PER": None, "종목명확인": None,
            "업종명": None, "업종평균PER": None, "업종비교군수": 0}
    try:
        res = requests.get(url, headers=NAVER_HEADERS, timeout=20)
        if debug:
            print(f"    [진단] integration status={res.status_code}")
        if res.status_code != 200:
            return info
        data = res.json()
    except Exception as e:
        if debug:
            print(f"    [진단] integration 실패: {e}")
        return info

    info["종목명확인"] = data.get("stockName")
    for row in data.get("totalInfos", []):
        code = row.get("code")
        value = row.get("value")
        if code == "per":
            info["네이버PER"] = to_number(re.sub(r"[^\d.\-]", "", str(value)))
        elif code == "cnsPer":
            info["추정PER"] = to_number(re.sub(r"[^\d.\-]", "", str(value)))
        elif code == "marketValue":
            info["시가총액"] = parse_korean_amount(value)
            if debug:
                print(f"    [진단] 시총 원문='{value}' → {info['시가총액']}")
        elif code == "lastClosePrice":
            info["현재가"] = to_number(value)

    # 이 종목의 PER을 캐시에 넣어 둔다.
    # 다른 종목의 비교군에 이 종목이 들어올 때 다시 조회하지 않아도 된다.
    if ticker not in _PEER_PER_CACHE:
        _PEER_PER_CACHE[ticker] = (info["네이버PER"]
                                   if is_valid_per(info["네이버PER"]) else None)

    # 동종업계 PER 기준 만들기.
    # industryCompareInfo는 dict가 아니라 '경쟁사 목록(리스트)'으로 온다.
    # 항목에는 주가·시총만 있고 PER이 없어서, 코드만 뽑아 따로 조회한다.
    cmp_info = data.get("industryCompareInfo")
    peer_codes = []
    if isinstance(cmp_info, list):
        for item in cmp_info:
            if not isinstance(item, dict):
                continue
            code = item.get("itemCode") or item.get("reutersCode")
            if code and code != ticker and code not in peer_codes:
                peer_codes.append(str(code))
    elif isinstance(cmp_info, dict):
        # 혹시 구조가 바뀌어 dict로 오면 평균 PER 필드를 직접 찾아본다
        info["업종명"] = (cmp_info.get("industryName")
                       or cmp_info.get("industryCodeName")
                       or cmp_info.get("name"))
        for key in ("industryPer", "upjongPer", "averagePer", "industryAvgPer"):
            v = to_number(re.sub(r"[^\d.\-]", "", str(cmp_info.get(key))))
            if v and v > 0:
                info["업종평균PER"] = v
                break
        for item in (cmp_info.get("stocks") or cmp_info.get("items") or []):
            if isinstance(item, dict):
                code = item.get("itemCode") or item.get("reutersCode")
                if code and code != ticker:
                    peer_codes.append(str(code))

    if info["업종평균PER"] is None and peer_codes:
        med, n_valid = industry_per_from_peers(peer_codes, debug=debug)
        if med:
            info["업종평균PER"] = med
            info["업종비교군수"] = n_valid

    if info.get("업종명") is None:
        info["업종명"] = (data.get("industryCodeType")
                       or data.get("industryCode")
                       or data.get("industryName"))

    if debug:
        print(f"    [진단] 업종={info['업종명']} 비교군={len(peer_codes)}개 "
              f"동종업계PER={info['업종평균PER']}")
    return info


def fetch_naver_basic_price(ticker):
    """네이버 basic API → 현재가 (주가 이력 API가 실패했을 때의 대체 경로)."""
    try:
        res = requests.get(f"https://m.stock.naver.com/api/stock/{ticker}/basic",
                           headers=NAVER_HEADERS, timeout=20)
        if res.status_code != 200:
            return None
        return to_number(res.json().get("closePrice"))
    except Exception:
        return None


# ── 6) 펀더멘탈 지표 계산 ─────────────────────────────
def compute_fundamentals(dart_history, naver_annual, current_per,
                         current_price=None, industry_per=None):
    """연도별 지표를 만들고 5개 축으로 펀더멘탈 점수를 산출한다.

    지표별 출처와 방식:
      PER        네이버 연도별 + 현재 PER(TTM) → 업종평균 대비(우선) / 자기과거(대체)
      영업이익률   DART 최대 12년 → 자기 과거 백분위
      부채비율    DART 최대 12년 (부채총계÷자본총계) → 절대 기준
      매출성장률   DART 최대 12년 → 절대 기준
      시가배당률   네이버 주당배당금 ÷ 현재주가 → 절대 기준
    """
    series = {"영업이익률": {}, "ROE": {}, "매출성장률": {}, "PER": {}, "부채비율": {}}

    # ── DART 기반 장기 지표 ──
    for year in sorted(dart_history.keys()):
        acc = dart_history[year]
        revenue = acc.get("매출액")
        op_profit = acc.get("영업이익")
        net_income = acc.get("당기순이익")
        equity = acc.get("자본총계")
        debt = acc.get("부채총계")

        if revenue and revenue > 0 and op_profit is not None:
            series["영업이익률"][year] = round(op_profit / revenue * 100, 2)
        if equity and equity > 0 and net_income is not None:
            series["ROE"][year] = round(net_income / equity * 100, 2)
        if equity and equity > 0 and debt is not None:
            series["부채비율"][year] = round(debt / equity * 100, 2)
        prev = dart_history.get(year - 1, {}).get("매출액")
        if prev and prev > 0 and revenue is not None:
            series["매출성장률"][year] = round((revenue - prev) / prev * 100, 2)

    # DART가 비면 네이버 값으로 대체 (연수는 짧지만 없는 것보단 낫다)
    for metric in ["영업이익률", "ROE", "부채비율"]:
        if not series[metric] and naver_annual.get(metric):
            series[metric] = dict(naver_annual[metric])
    if not series["매출성장률"] and naver_annual.get("매출액"):
        rev = naver_annual["매출액"]
        for year in sorted(rev):
            if year - 1 in rev and rev[year - 1]:
                series["매출성장률"][year] = round(
                    (rev[year] - rev[year - 1]) / rev[year - 1] * 100, 2)

    # PER은 유효 범위 안의 값만 쓴다
    raw_per = naver_annual.get("PER") or {}
    series["PER"] = {y: v for y, v in raw_per.items() if is_valid_per(v)}
    dropped_per = {y: v for y, v in raw_per.items() if not is_valid_per(v)}

    # ── 각 지표의 '현재값' 확정 ──
    current = {}
    percentiles = {}
    for metric in ["영업이익률", "ROE", "매출성장률", "부채비율"]:
        values = series[metric]
        if values:
            latest = max(values)
            current[metric] = values[latest]
            past = [v for y, v in values.items() if y != latest]
            percentiles[metric] = percentile_rank(values[latest], past)
        else:
            current[metric] = None
            percentiles[metric] = None

    current["PER"] = current_per if is_valid_per(current_per) else None
    current["PER원본"] = current_per
    if current["PER"] is not None and len(series["PER"]) >= MIN_VALID_PER_SAMPLES:
        percentiles["PER"] = percentile_rank(current["PER"],
                                             list(series["PER"].values()))
    else:
        percentiles["PER"] = None

    # 시가배당률 = 최근 주당배당금 ÷ 현재 주가
    dividend = None
    dps_series = naver_annual.get("주당배당금") or {}
    if dps_series and current_price and current_price > 0:
        latest_dps = dps_series[max(dps_series)]
        if latest_dps is not None:
            dividend = round(latest_dps / current_price * 100, 2)
    current["시가배당률"] = dividend

    # ── 5개 축 점수화 ──
    per_score, per_note = score_per(current_per, industry_per,
                                    list(series["PER"].values()))
    parts = {
        "PER": per_score,
        "영업이익률": percentiles["영업이익률"],
        "부채비율": score_debt_ratio(current["부채비율"]),
        "매출성장률": score_revenue_growth(current["매출성장률"]),
        "시가배당률": score_dividend_yield(dividend),
    }
    funda_score, covered_weight = combine_funda_score(parts)

    주의 = []
    if (current["부채비율"] is not None
            and current["부채비율"] > DEBT_RATIO_UNSCORABLE):
        주의.append(f"부채비율 {current['부채비율']:.0f}% — 금융업이면 정상, "
                  f"아니면 재무 위험. 점수에서 제외함")
    if industry_per is None and len(series["PER"]) < MIN_VALID_PER_SAMPLES:
        주의.append("PER 비교 기준 없음")

    profit_years = series["영업이익률"] or series["ROE"]
    return {
        "연도별": series,
        "현재": current,
        "백분위": percentiles,
        "축점수": parts,
        "주의": 주의,
        "펀더멘탈점수": funda_score,
        "반영배점": covered_weight,
        "PER근거": per_note,
        "업종평균PER": industry_per,
        "기준연도": max(profit_years) if profit_years else None,
        "이력연수": len(profit_years),
        "PER이력연수": len(series["PER"]),
        "부채이력연수": len(series["부채비율"]),
        "PER제외연도": dropped_per,
        "PER판정불가사유": None if per_score is not None else per_note,
    }


def classify(narrative_score, narrative_sources, funda, supply_signal):
    """세 신호를 합쳐 분류 라벨과 종합 점수를 만든다.

    이전 버전은 PER 하나로 '싼가'를 판단했다. 지금은 수익성·안정성·성장성·배당을
    함께 보는 펀더멘탈 점수를 쓴다. 따라서 골든존의 의미도
    '싼 종목'에서 '체력이 좋은 종목'으로 넓어졌다.
    """
    cur = funda["현재"]
    funda_score = funda.get("펀더멘탈점수")

    # 적자 기업은 펀더멘탈 판정 대상이 아니다.
    # 지표가 낮아 보여도 이익이 없으면 '좋다/나쁘다'를 논할 수 없다.
    op_margin = cur.get("영업이익률")
    roe = cur.get("ROE")
    is_loss = (op_margin is not None and op_margin <= 0) or \
              (roe is not None and roe <= 0)

    is_strong = (not is_loss
                 and funda_score is not None
                 and funda_score >= FUNDA_GOOD_THRESHOLD)
    is_hot = (narrative_sources >= NARRATIVE_MIN_SOURCES
              and narrative_score >= NARRATIVE_MIN_SCORE)
    has_supply = supply_signal is not None

    # 수익성이 자기 과거 대비 어디쯤인지 (골든존 세부 구분용)
    profit_pcts = [funda["백분위"][m] for m in ("영업이익률", "ROE")
                   if funda["백분위"].get(m) is not None]
    avg_profit_pct = statistics.mean(profit_pcts) if profit_pcts else None

    if is_loss:
        label = "🚫 적자 (펀더멘탈 판정 제외)"
        note = "영업이익 또는 순이익이 적자 — 펀더멘탈 점수를 논할 수 없음"
    elif funda_score is None:
        label = "❔ 펀더멘탈 판정 보류"
        note = "지표를 하나도 확보하지 못함"
    elif is_strong and is_hot:
        if avg_profit_pct is not None and avg_profit_pct < 40:
            label = "🎯 골든존 (사이클 저점형)"
            note = "펀더멘탈은 받쳐주는데 수익성은 과거 대비 낮은 구간 — 턴어라운드 가정 필요"
        else:
            label = "🎯 골든존 (체력 + 내러티브)"
            note = "펀더멘탈이 탄탄하고 시장의 관심도 붙은 구간"
    elif is_strong and not is_hot:
        label = "💎 숨은 우량 (아직 소외)"
        note = "펀더멘탈은 좋은데 아직 얘기가 안 되는 구간"
    elif is_hot and not is_strong:
        label = "🔥 내러티브 선행 (펀더멘탈 부담)"
        note = "얘기는 뜨거운데 펀더멘탈이 못 따라오는 구간"
    else:
        label = "⚪ 관망"
        note = ""

    # 종합 점수: 내러티브 40% + 펀더멘탈 40% + 수급 20점
    parts = [narrative_score * 0.4]
    if funda_score is not None and not is_loss:
        parts.append(funda_score * 0.4)
    parts.append(20 if has_supply else 0)
    total = round(sum(parts), 1)

    return label, note, total, funda_score, avg_profit_pct


# ── 8) 텔레그램 ───────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[텔레그램 미설정] 전송 생략")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # 텔레그램 메시지 길이 제한(4096자) 대응
    for chunk_start in range(0, len(message), 3800):
        chunk = message[chunk_start:chunk_start + 3800]
        try:
            requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=20)
            time.sleep(0.3)
        except Exception as e:
            print(f"[텔레그램 오류] {e}")
            return


def fmt(value, suffix=""):
    return f"{value}{suffix}" if value is not None else "-"


# ── 메인 ─────────────────────────────────────────────
def main():
    if not DART_API_KEY:
        print("[오류] DART_API_KEY 환경변수가 없습니다.")
        return

    raw = collect_narrative_raw()
    if not raw:
        print("분석할 종목이 없습니다. 피드 담당자를 먼저 실행하세요.")
        return

    ticker_map = load_json(TICKER_MAP_FILE, {})
    if not ticker_map:
        msg = "⚠️ <b>매칭 엔진 경고</b>\nticker_name_map.json 이 비어 있습니다. 먼저 생성해주세요."
        print(msg)
        send_telegram(msg)
        return

    corp_map = build_ticker_to_corpcode()
    print(f"DART 기업코드 매핑: {len(corp_map):,}개")

    # 티커 → 정식 종목명 (표시용)
    ticker_to_official = {v: k for k, v in ticker_map.items()}

    # 종목명을 티커로 바꾸면서 같은 티커끼리 합친다.
    # '한조'와 'HD한국조선해양'처럼 다른 이름으로 언급돼도 한 종목으로 묶여야
    # 출처 수가 쪼개지지 않고, 같은 종목을 두 번 조회하지도 않는다.
    by_ticker = {}
    unmatched = []
    for name, entry in raw.items():
        ticker = match_name_to_ticker(name, ticker_map)
        if not ticker:
            unmatched.append(name)
            continue
        by_ticker[ticker] = (merge_raw(by_ticker[ticker], entry)
                             if ticker in by_ticker else entry)

    narrative = {t: score_narrative(e) for t, e in by_ticker.items()}
    merged_count = len(raw) - len(unmatched) - len(narrative)
    print(f"최근 {LOOKBACK_DAYS_FOR_MENTIONS}일간 언급: "
          f"이름 {len(raw)}개 → 종목 {len(narrative)}개"
          + (f" (별칭 {merged_count}건 병합)" if merged_count > 0 else ""))

    supply = load_supply_signals()
    print(f"최근 수급 신호 보유 종목: {len(supply)}개")

    # 지분 정보는 판정 점수에 반영하지 않고 '맥락 플래그'로만 붙인다.
    # 지분 구조는 매수 타이밍 신호가 아니라 배경 조건이기 때문이다.
    ownership = load_json(OWNERSHIP_FILE, {})
    print(f"지분 정보 보유 종목: {len(ownership)}개")

    cache = load_json(FUNDA_CACHE_FILE, {})
    cache_ttl_days = 7

    results = []

    for ticker, narr in sorted(narrative.items(),
                               key=lambda kv: -kv[1]["내러티브점수"]):
        name = ticker_to_official.get(ticker) or narr["언급된이름"][0]
        corp_code = corp_map.get(ticker)
        if not corp_code:
            unmatched.append(f"{name}(기업코드없음)")
            continue

        debug = len(results) < DEBUG_FIRST_N
        print(f"\n▶ {name} ({ticker})")

        # 재무 이력은 자주 안 바뀌므로 캐시 재사용
        cached = cache.get(ticker)
        cache_fresh = False
        if cached:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["_cached_at"])
                cache_fresh = age < timedelta(days=cache_ttl_days)
            except Exception:
                cache_fresh = False

        if cache_fresh:
            dart_history = {int(k): v for k, v in cached["dart"].items()}
            print("  재무 이력: 캐시 사용")
        else:
            print("  재무 이력: DART 조회 중...")
            dart_history = fetch_dart_history(corp_code)
            cache[ticker] = {
                "_cached_at": datetime.now(timezone.utc).isoformat(),
                "dart": {str(k): v for k, v in dart_history.items()},
            }

        # 네이버: 연도별 PER 이력 + 현재 시세/PER
        naver_annual = fetch_naver_annual_metrics(ticker, debug=debug)
        naver_now = fetch_naver_current(ticker, debug=debug)
        time.sleep(0.3)

        current_price = naver_now["현재가"] or fetch_naver_basic_price(ticker)
        current_per = naver_now["네이버PER"]

        funda = compute_fundamentals(dart_history, naver_annual, current_per,
                                     current_price, naver_now.get("업종평균PER"))

        if funda["이력연수"] < MIN_HISTORY_YEARS and funda["펀더멘탈점수"] is None:
            print(f"  이력 부족 (재무 {funda['이력연수']}년, 지표 확보 실패) → 판단 제외")
            results.append({
                "종목명": name, "티커": ticker,
                "상태": "이력부족",
                "이력연수": funda["이력연수"],
                "내러티브": narr,
            })
            continue

        own = ownership.get(ticker)

        supply_signal = None
        for alias in list(narr["언급된이름"]) + [name]:
            if alias in supply:
                supply_signal = supply[alias]
                break
        label, note, total, val_score, profit_pct = classify(
            narr["내러티브점수"], narr["출처수"], funda, supply_signal)

        # 무엇이 빠졌는지 바로 알 수 있게 재료 상태를 함께 출력
        print(f"  재료: 수익성 {funda['이력연수']}년(DART) · "
              f"PER {funda['PER이력연수']}년(네이버) · "
              f"현재PER {'O' if current_per else 'X'}")
        s = funda["축점수"]
        print(f"  {label} | 종합 {total}점 | "
              f"펀더멘탈 {fmt(funda['펀더멘탈점수'])}점 "
              f"(반영배점 {funda['반영배점']}/100)")
        print(f"    PER {fmt(funda['현재']['PER'])} → {fmt(s['PER'])}점 "
              f"[{funda['PER근거']}]")
        print(f"    영업이익률 {fmt(funda['현재']['영업이익률'], '%')} → {fmt(s['영업이익률'])}점 "
              f"(자기 {funda['이력연수']}년 백분위) · "
              f"부채비율 {fmt(funda['현재']['부채비율'], '%')} → {fmt(s['부채비율'])}점")
        print(f"    매출성장 {fmt(funda['현재']['매출성장률'], '%')} → {fmt(s['매출성장률'])}점 · "
              f"배당 {fmt(funda['현재']['시가배당률'], '%')} → {fmt(s['시가배당률'])}점")
        for w in funda.get("주의", []):
            print(f"    ⚠️ {w}")
        if own and own.get("플래그"):
            print(f"  지분: {' · '.join(own['플래그'])}")

        results.append({
            "종목명": name,
            "티커": ticker,
            "상태": "분석완료",
            "라벨": label,
            "설명": note,
            "종합점수": total,
            "현재가": current_price,
            "내러티브": narr,
            "펀더멘탈": funda,
            "펀더멘탈점수": val_score,
            "수익성백분위평균": round(profit_pct, 1) if profit_pct is not None else None,
            "수급신호": supply_signal["신호"] if supply_signal else None,
            "참고_네이버PER": naver_now["네이버PER"],
            "참고_추정PER": naver_now["추정PER"],
            "지분플래그": own.get("플래그") if own else None,
            "지분지표": own.get("지표") if own else None,
        })

    save_json(FUNDA_CACHE_FILE, cache)

    analyzed = [r for r in results if r["상태"] == "분석완료"]
    analyzed.sort(key=lambda r: -r["종합점수"])

    payload = {
        "생성시각": datetime.now(timezone.utc).isoformat(),
        "기준": {
            "언급조회기간일": LOOKBACK_DAYS_FOR_MENTIONS,
            "내러티브하한": NARRATIVE_MIN_SCORE,
            "PER저평가백분위": VALUATION_MAX_PERCENTILE,
        },
        "배점": FUNDA_WEIGHTS,
        "주의": ("펀더멘탈 점수는 5개 축의 가중 평균이며, 확보된 지표의 배점만으로 환산한다. "
                "부채비율 절대기준은 금융업(증권·은행·보험)에 맞지 않는다. "
                "PER 이력은 네이버 연도별 값으로 보통 3년뿐이라 자기과거 방식은 해상도가 거칠다. "
                f"PER {PER_VALID_MIN}~{PER_VALID_MAX} 범위 밖(적자·이익 미미)은 제외. "
                "영업이익률·ROE·매출성장률은 DART 연간 재무로 계산(최대 12년), "
                "자본총계는 연결 기준."),
        "결과": analyzed,
        "이력부족": [r for r in results if r["상태"] == "이력부족"],
        "미매칭": unmatched,
    }
    save_json(RESULTS_FILE, payload)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": payload["생성시각"],
            "골든존": [r["종목명"] for r in analyzed if "골든존" in r["라벨"]],
            "분석종목수": len(analyzed),
        }, ensure_ascii=False) + "\n")

    # ── 텔레그램 리포트 ──
    golden = [r for r in analyzed if "골든존" in r["라벨"]]
    hidden = [r for r in analyzed if "숨은 우량" in r["라벨"]]

    lines = [f"🎯 <b>매칭 엔진 리포트</b> "
             f"({datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST)",
             f"분석 {len(analyzed)}종목 · 골든존 {len(golden)}개\n"]

    if golden:
        lines.append("━━━ <b>골든존</b> ━━━")
        for r in golden:
            f_ = r["펀더멘탈"]
            lines.append(
                f"\n<b>{r['종목명']}</b> ({r['티커']}) · {r['종합점수']}점\n"
                f"{r['라벨']}\n"
                f"  · 펀더멘탈 {fmt(f_['펀더멘탈점수'])}점 "
                f"(반영배점 {f_['반영배점']}/100)\n"
                f"  · PER {fmt(f_['현재']['PER'])} · "
                f"영업이익률 {fmt(f_['현재']['영업이익률'], '%')} · "
                f"부채비율 {fmt(f_['현재']['부채비율'], '%')}\n"
                f"  · 매출성장 {fmt(f_['현재']['매출성장률'], '%')} · "
                f"배당 {fmt(f_['현재']['시가배당률'], '%')}\n"
                f"  · 내러티브 {r['내러티브']['내러티브점수']}점 "
                f"({r['내러티브']['출처수']}개 출처, {r['내러티브']['언급건수']}건)"
            )
            if r.get("지분플래그"):
                lines.append(f"  · 지분: {' · '.join(r['지분플래그'])}")
            names = r["내러티브"].get("언급된이름") or []
            if len(names) > 1:
                lines.append(f"  · 언급된 이름: {' / '.join(names)}")
            if r["수급신호"]:
                lines.append(f"  · 수급: {', '.join(r['수급신호'][:2])}")
            if r["내러티브"]["근거"]:
                lines.append(f"  · {r['내러티브']['근거'][0][:110]}")
    else:
        # 왜 0개인지 구분해서 알려준다 (조건 미달 vs 데이터 부족)
        with_per = [r for r in analyzed
                    if r["펀더멘탈"].get("펀더멘탈점수") is not None]
        hot = [r for r in analyzed
               if r["내러티브"]["출처수"] >= NARRATIVE_MIN_SOURCES
               and r["내러티브"]["내러티브점수"] >= NARRATIVE_MIN_SCORE]
        loss = [r for r in analyzed if "적자" in r["라벨"]]
        hold = [r for r in analyzed if "보류" in r["라벨"]]
        lines.append("오늘은 골든존 조건을 만족한 종목이 없습니다.")
        lines.append(f"  · 펀더멘탈 점수 산출: {len(with_per)}/{len(analyzed)}"
                     f" (적자 {len(loss)}개, 판정보류 {len(hold)}개 제외)")
        lines.append(f"  · 내러티브 통과: {len(hot)}/{len(analyzed)}")
        if len(hot) <= 1:
            lines.append("  → 내러티브 데이터가 아직 얇습니다. "
                         "매일 자동 실행이 2주쯤 쌓여야 여러 출처의 겹침이 잡힙니다.")

    if hidden:
        lines.append("\n━━━ <b>숨은 우량 (아직 소외)</b> ━━━")
        for r in hidden[:5]:
            f_ = r["펀더멘탈"]
            line = (f"· {r['종목명']} — 펀더멘탈 {fmt(f_['펀더멘탈점수'])}점 "
                    f"(PER {fmt(f_['현재']['PER'])})")
            fr = (r.get("지분지표") or {}).get("실질유통비율")
            if fr is not None:
                line += f" · 유통 {fr}%"
            lines.append(line)

    lines.append(f"\n<i>※ 펀더멘탈 = PER25 + 영업이익률25 + 부채비율20 + 매출성장15 + 배당15. "
                 f"부채비율 절대기준은 금융업에 맞지 않습니다.</i>")

    send_telegram("\n".join(lines))

    print(f"\n{'='*50}")
    print(f"분석 완료: {len(analyzed)}종목 / 골든존 {len(golden)}개")
    got = {}
    for axis in FUNDA_WEIGHTS:
        got[axis] = sum(1 for r in analyzed
                        if (r["펀더멘탈"].get("축점수") or {}).get(axis) is not None)
    ind = sum(1 for r in analyzed if r["펀더멘탈"].get("업종평균PER"))
    print(f"동종업계 PER 기준 확보: {ind}/{len(analyzed)}종목 "
          f"(나머지는 자기 과거 PER로 대체)")
    print(f"축별 점수 산출: " +
          " · ".join(f"{k} {v}/{len(analyzed)}" for k, v in got.items()))
    if unmatched:
        print(f"미매칭: {len(unmatched)}개 — {', '.join(unmatched[:10])}")


if __name__ == "__main__":
    main()
