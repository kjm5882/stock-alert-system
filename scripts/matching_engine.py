"""
매칭 엔진 (Matching Engine)
==========================
목적: 세 담당자의 신호를 교차해서 "골든존" 종목을 찾아낸다.

  📰 내러티브 (피드 담당자)  : 요즘 블로거/유튜버가 많이, 긍정적으로 얘기하는가
  📊 펀더멘탈 (이 스크립트)  : 그 종목의 현재 지표가 자기 과거 10년 대비 어디쯤인가
  💹 수급     (수급 담당자)  : 외국인/기관이 실제로 사고 있는가

데이터 출처 (KRX 차단 이슈로 pykrx 미사용):
  - DART 연간 재무   : 매출액/영업이익/당기순이익/자본총계 (최대 12년)
  - DART 주식총수    : 발행주식수 (과거 EPS를 수정주가 기준으로 환산할 때 사용)
  - 네이버 일봉 차트 : 2016년~현재 종가 (연도별 연말 종가 추출)
  - 네이버 integration: 현재 주가, 네이버 자체 PER(TTM) — 참고용

지표 계산 (모두 DART 연간 데이터로 통일해서 방법론 일관성 유지):
  - 영업이익률 = 영업이익 / 매출액
  - ROE       = 당기순이익 / 자본총계
  - 매출성장률 = 전년 대비 매출 증감률
  - PER       = 그 해 연말 종가 / (그 해 당기순이익 / 현재 발행주식수)
                * 현재 PER은 같은 공식에 '현재 주가'만 대입 → 과거와 동일 기준

⚠️ 알려진 한계 (결과에 함께 표기됨)
  - 과거 EPS를 '현재' 발행주식수로 환산하므로, 그 사이 유상증자·자사주 소각 등으로
    주식 수가 크게 변한 기업은 과거 PER에 오차가 생긴다.
  - 자본총계는 연결 기준(비지배지분 포함)이라 ROE가 약간 낮게 나올 수 있다.
  - 이력이 3년 미만인 종목(신규 상장 등)은 '이력부족'으로 표시하고 판단에서 제외한다.
"""

import os
import re
import json
import time
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

# ── 설정 ──────────────────────────────────────────────
LOOKBACK_DAYS_FOR_MENTIONS = 14   # 피드에서 며칠치 언급을 볼지
HISTORY_START_YEAR = 2016         # 주가 이력 시작 (네이버 제공 범위)
MIN_HISTORY_YEARS = 3             # 이력이 이보다 적으면 판단 제외

# 골든존 기준
NARRATIVE_MIN_SOURCES = 2         # 서로 다른 블로거/채널 최소 몇 곳에서 언급돼야 하는지
NARRATIVE_MIN_SCORE = 50          # 내러티브 점수 하한
VALUATION_MAX_PERCENTILE = 40     # PER이 과거 분포의 이 백분위 이하면 '저평가'

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FEED_SIGNALS_FILE = os.path.join(DATA_DIR, "feed_signals.jsonl")
SUPPLY_SIGNALS_FILE = os.path.join(DATA_DIR, "supply_demand_signals.jsonl")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_name_map.json")
CORP_CODES_FILE = os.path.join(DATA_DIR, "corp_codes.xml")
FUNDA_CACHE_FILE = os.path.join(DATA_DIR, "fundamental_cache.json")
RESULTS_FILE = os.path.join(DATA_DIR, "matching_results.json")
HISTORY_FILE = os.path.join(DATA_DIR, "matching_history.jsonl")

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


def percentile_rank(value, history):
    """value가 history 분포에서 몇 번째 백분위인지 (0~100). 낮을수록 분포 하단."""
    if value is None or not history:
        return None
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return round((below + 0.5 * equal) / len(history) * 100, 1)


# ── 1) 내러티브 점수 ──────────────────────────────────
def build_narrative_scores():
    """feed_signals.jsonl → 종목별 내러티브 점수와 근거.

    점수 구성 (최대 100점):
      - 서로 다른 출처(블로그/채널) 수 : 1곳 10점 → 2곳 28점 → 3곳 46점 → 4곳 이상 60점
      - 긍정 논조 비율                : 최대 25점
      - 확신도 '높음' 비율            : 최대 15점

    출처 1곳짜리 언급은 의도적으로 낮게 잡는다. 한 블로거가 한 번 언급한 것은
    '내러티브가 형성됐다'고 보기 어렵고, 여러 사람이 동시에 얘기하는 것이 신호이기 때문.
    """
    if not os.path.exists(FEED_SIGNALS_FILE):
        print("[알림] feed_signals.jsonl 이 없습니다. 피드 담당자를 먼저 실행하세요.")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS_FOR_MENTIONS)
    per_stock = {}

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

            entry = per_stock.setdefault(name, {
                "언급건수": 0, "출처": set(), "긍정": 0, "부정": 0, "중립": 0,
                "확신도높음": 0, "근거": [],
            })
            entry["언급건수"] += 1
            entry["출처"].add(rec.get("source_name", "?"))
            tone = rec.get("논조", "중립")
            entry[tone if tone in ("긍정", "부정", "중립") else "중립"] += 1
            if rec.get("확신도") == "높음":
                entry["확신도높음"] += 1
            reason = rec.get("언급이유")
            if reason and len(entry["근거"]) < 3:
                entry["근거"].append(f"[{rec.get('source_name')}] {reason}")

    scores = {}
    for name, e in per_stock.items():
        n = e["언급건수"]
        sources = len(e["출처"])
        source_score = min(60, 10 + (sources - 1) * 18)
        positive_score = (e["긍정"] / n) * 25 if n else 0
        confidence_score = (e["확신도높음"] / n) * 15 if n else 0
        total = round(source_score + positive_score + confidence_score, 1)

        scores[name] = {
            "내러티브점수": total,
            "언급건수": n,
            "출처수": len(e["출처"]),
            "출처목록": sorted(e["출처"]),
            "논조": {"긍정": e["긍정"], "중립": e["중립"], "부정": e["부정"]},
            "근거": e["근거"],
        }
    return scores


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
DART_ACCOUNTS = {"매출액", "영업이익", "당기순이익", "자본총계"}


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


def fetch_shares_outstanding(corp_code):
    """DART 주식총수 → 보통주 발행주식총수."""
    url = "https://opendart.fss.or.kr/api/stockTotqySttus.json"
    current_year = datetime.now().year
    for year in [current_year - 1, current_year - 2]:
        try:
            res = requests.get(url, params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": "11011",
            }, timeout=30)
            data = res.json()
        except Exception:
            continue
        if data.get("status") != "000":
            continue
        for row in data.get("list", []):
            se = (row.get("se") or "").strip()
            if "보통주" in se:
                shares = to_number(row.get("istc_totqy"))
                if shares and shares > 0:
                    return shares
        time.sleep(0.15)
    return None


# ── 5) 네이버 주가 이력 / 현재 정보 ───────────────────
def fetch_yearly_close_prices(ticker):
    """네이버 일봉 → {연도: 그 해 마지막 거래일 종가}. 수정주가 기준."""
    today = datetime.now().strftime("%Y%m%d")
    url = (f"https://api.stock.naver.com/chart/domestic/item/{ticker}/day"
           f"?startDateTime={HISTORY_START_YEAR}01010000&endDateTime={today}0000")
    try:
        res = requests.get(url, headers=NAVER_HEADERS, timeout=30)
        if res.status_code != 200:
            return {}, None
        rows = res.json()
    except Exception as e:
        print(f"    [주가 이력 오류] {ticker}: {e}")
        return {}, None

    if not isinstance(rows, list) or not rows:
        return {}, None

    yearly = {}
    for row in rows:
        date_str = str(row.get("localDate", ""))
        close = row.get("closePrice")
        if len(date_str) != 8 or close is None:
            continue
        year = int(date_str[:4])
        # 나중 날짜가 덮어쓰므로 결과적으로 그 해 마지막 거래일 종가가 남는다
        yearly[year] = float(close)

    latest_close = float(rows[-1].get("closePrice")) if rows[-1].get("closePrice") else None
    return yearly, latest_close


def fetch_naver_current(ticker):
    """네이버 integration → 현재가, 네이버 자체 PER(TTM) 등 참고 지표."""
    url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
    info = {"현재가": None, "네이버PER": None, "추정PER": None, "종목명확인": None}
    try:
        res = requests.get(url, headers=NAVER_HEADERS, timeout=20)
        if res.status_code != 200:
            return info
        data = res.json()
    except Exception:
        return info

    info["종목명확인"] = data.get("stockName")
    for row in data.get("totalInfos", []):
        code = row.get("code")
        value = row.get("value")
        if code == "per":
            info["네이버PER"] = to_number(re.sub(r"[^\d.\-]", "", str(value)))
        elif code == "cnsPer":
            info["추정PER"] = to_number(re.sub(r"[^\d.\-]", "", str(value)))
    return info


# ── 6) 펀더멘탈 지표 계산 ─────────────────────────────
def compute_fundamentals(dart_history, yearly_prices, shares, current_price):
    """연도별 지표와 현재 지표를 계산하고, 현재값의 과거 대비 백분위를 낸다."""
    years = sorted(dart_history.keys())
    series = {"영업이익률": {}, "ROE": {}, "매출성장률": {}, "PER": {}}

    for year in years:
        acc = dart_history[year]
        revenue = acc.get("매출액")
        op_profit = acc.get("영업이익")
        net_income = acc.get("당기순이익")
        equity = acc.get("자본총계")

        if revenue and revenue > 0 and op_profit is not None:
            series["영업이익률"][year] = round(op_profit / revenue * 100, 2)
        if equity and equity > 0 and net_income is not None:
            series["ROE"][year] = round(net_income / equity * 100, 2)
        prev = dart_history.get(year - 1, {}).get("매출액")
        if prev and prev > 0 and revenue is not None:
            series["매출성장률"][year] = round((revenue - prev) / prev * 100, 2)
        # 과거 PER: 그 해 연말 종가 ÷ (그 해 순이익 / 현재 주식수)
        if shares and net_income and net_income > 0 and year in yearly_prices:
            eps = net_income / shares
            if eps > 0:
                series["PER"][year] = round(yearly_prices[year] / eps, 2)

    latest_year = max(years) if years else None
    current = {}
    percentiles = {}

    for metric in ["영업이익률", "ROE", "매출성장률"]:
        values = series[metric]
        if latest_year in values:
            current[metric] = values[latest_year]
            past = [v for y, v in values.items() if y != latest_year]
            percentiles[metric] = percentile_rank(values[latest_year], past)
        else:
            current[metric] = None
            percentiles[metric] = None

    # 현재 PER: 과거와 동일한 공식에 '현재 주가'만 대입
    current_per = None
    if shares and current_price and latest_year:
        net_income = dart_history[latest_year].get("당기순이익")
        if net_income and net_income > 0:
            eps = net_income / shares
            if eps > 0:
                current_per = round(current_price / eps, 2)
    current["PER"] = current_per
    past_per = [v for y, v in series["PER"].items() if y != latest_year]
    percentiles["PER"] = percentile_rank(current_per, past_per)

    return {
        "연도별": {m: series[m] for m in series},
        "현재": current,
        "백분위": percentiles,
        "기준연도": latest_year,
        "이력연수": len(years),
    }


# ── 7) 종합 판정 ──────────────────────────────────────
def classify(narrative_score, narrative_sources, funda, supply_signal):
    """세 신호를 합쳐 분류 라벨과 종합 점수를 만든다."""
    pcts = funda["백분위"]
    per_pct = pcts.get("PER")
    profit_pcts = [pcts[m] for m in ("영업이익률", "ROE") if pcts.get(m) is not None]
    avg_profit_pct = statistics.mean(profit_pcts) if profit_pcts else None

    # 밸류에이션 점수: PER이 과거 분포 하단일수록 높음
    valuation_score = (100 - per_pct) if per_pct is not None else None

    # 라벨 결정
    is_cheap = per_pct is not None and per_pct <= VALUATION_MAX_PERCENTILE
    # 내러티브는 '여러 곳에서 동시에' 얘기될 때만 인정 (단일 블로그 1회 언급은 제외)
    is_hot = (narrative_sources >= NARRATIVE_MIN_SOURCES
              and narrative_score >= NARRATIVE_MIN_SCORE)
    has_supply = supply_signal is not None

    if is_cheap and is_hot:
        if avg_profit_pct is not None and avg_profit_pct < 40:
            label = "🎯 골든존 (사이클 저점형)"
            note = "밸류에이션도 실적도 과거 대비 낮은 구간 — 턴어라운드 가정이 맞아야 성립"
        else:
            label = "🎯 골든존 (실적 유지 + 저평가)"
            note = "실적은 과거 수준을 지키는데 밸류만 낮아진 구간"
    elif is_cheap and not is_hot:
        label = "💎 숨은 저평가 (아직 소외)"
        note = "밸류는 싼데 아직 얘기가 안 되는 구간"
    elif is_hot and not is_cheap:
        label = "🔥 내러티브 선행 (밸류 부담)"
        note = "얘기는 뜨거운데 밸류는 과거 대비 높은 구간"
    else:
        label = "⚪ 관망"
        note = ""

    # 종합 점수: 내러티브 40% + 밸류 40% + 수급 20%
    parts = [narrative_score * 0.4]
    if valuation_score is not None:
        parts.append(valuation_score * 0.4)
    parts.append(20 if has_supply else 0)
    total = round(sum(parts), 1)

    return label, note, total, valuation_score, avg_profit_pct


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

    narrative = build_narrative_scores()
    print(f"최근 {LOOKBACK_DAYS_FOR_MENTIONS}일간 언급 종목: {len(narrative)}개")
    if not narrative:
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

    supply = load_supply_signals()
    print(f"최근 수급 신호 보유 종목: {len(supply)}개")

    cache = load_json(FUNDA_CACHE_FILE, {})
    cache_ttl_days = 7

    results = []
    unmatched = []

    for name, narr in sorted(narrative.items(),
                             key=lambda kv: -kv[1]["내러티브점수"]):
        ticker = match_name_to_ticker(name, ticker_map)
        if not ticker:
            unmatched.append(name)
            continue
        corp_code = corp_map.get(ticker)
        if not corp_code:
            unmatched.append(f"{name}(기업코드없음)")
            continue

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
            shares = cached["shares"]
            print("  재무 이력: 캐시 사용")
        else:
            print("  재무 이력: DART 조회 중...")
            dart_history = fetch_dart_history(corp_code)
            shares = fetch_shares_outstanding(corp_code)
            cache[ticker] = {
                "_cached_at": datetime.now(timezone.utc).isoformat(),
                "dart": {str(k): v for k, v in dart_history.items()},
                "shares": shares,
            }

        if len(dart_history) < MIN_HISTORY_YEARS:
            print(f"  이력 부족 ({len(dart_history)}년) → 판단 제외")
            results.append({
                "종목명": name, "티커": ticker,
                "상태": "이력부족",
                "이력연수": len(dart_history),
                "내러티브": narr,
            })
            continue

        yearly_prices, latest_close = fetch_yearly_close_prices(ticker)
        naver_now = fetch_naver_current(ticker)
        current_price = latest_close
        time.sleep(0.3)

        funda = compute_fundamentals(dart_history, yearly_prices, shares, current_price)
        supply_signal = supply.get(name)
        label, note, total, val_score, profit_pct = classify(
            narr["내러티브점수"], narr["출처수"], funda, supply_signal)

        print(f"  {label} | 종합 {total}점 | "
              f"PER {fmt(funda['현재']['PER'])} (백분위 {fmt(funda['백분위']['PER'])})")

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
            "밸류점수": val_score,
            "수익성백분위평균": round(profit_pct, 1) if profit_pct is not None else None,
            "수급신호": supply_signal["신호"] if supply_signal else None,
            "참고_네이버PER": naver_now["네이버PER"],
            "참고_추정PER": naver_now["추정PER"],
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
        "주의": ("과거 EPS를 현재 발행주식수로 환산했으므로 증자·소각이 많았던 기업은 "
                "과거 PER에 오차가 있을 수 있음. 자본총계는 연결 기준."),
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
    hidden = [r for r in analyzed if "숨은 저평가" in r["라벨"]]

    lines = [f"🎯 <b>매칭 엔진 리포트</b> "
             f"({datetime.now().strftime('%Y-%m-%d %H:%M')})",
             f"분석 {len(analyzed)}종목 · 골든존 {len(golden)}개\n"]

    if golden:
        lines.append("━━━ <b>골든존</b> ━━━")
        for r in golden:
            f_ = r["펀더멘탈"]
            lines.append(
                f"\n<b>{r['종목명']}</b> ({r['티커']}) · {r['종합점수']}점\n"
                f"{r['라벨']}\n"
                f"  · PER {fmt(f_['현재']['PER'])} "
                f"(과거 {f_['이력연수']}년 중 하위 {fmt(f_['백분위']['PER'], '%')})\n"
                f"  · 영업이익률 {fmt(f_['현재']['영업이익률'], '%')} "
                f"(백분위 {fmt(f_['백분위']['영업이익률'])})\n"
                f"  · ROE {fmt(f_['현재']['ROE'], '%')} "
                f"(백분위 {fmt(f_['백분위']['ROE'])})\n"
                f"  · 내러티브 {r['내러티브']['내러티브점수']}점 "
                f"({r['내러티브']['출처수']}개 출처, {r['내러티브']['언급건수']}건)"
            )
            if r["수급신호"]:
                lines.append(f"  · 수급: {', '.join(r['수급신호'][:2])}")
            if r["내러티브"]["근거"]:
                lines.append(f"  · {r['내러티브']['근거'][0][:110]}")
    else:
        lines.append("오늘은 골든존 조건을 만족한 종목이 없습니다.")

    if hidden:
        lines.append("\n━━━ <b>숨은 저평가 (아직 소외)</b> ━━━")
        for r in hidden[:5]:
            f_ = r["펀더멘탈"]
            lines.append(f"· {r['종목명']} — PER {fmt(f_['현재']['PER'])} "
                         f"(하위 {fmt(f_['백분위']['PER'], '%')})")

    lines.append(f"\n<i>※ 과거 PER은 현재 주식수 기준 환산값. "
                 f"증자·소각 이력이 큰 종목은 참고용.</i>")

    send_telegram("\n".join(lines))

    print(f"\n{'='*50}")
    print(f"분석 완료: {len(analyzed)}종목 / 골든존 {len(golden)}개")
    if unmatched:
        print(f"미매칭: {len(unmatched)}개 — {', '.join(unmatched[:10])}")


if __name__ == "__main__":
    main()
