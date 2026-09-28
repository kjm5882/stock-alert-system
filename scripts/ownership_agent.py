"""
지분 담당자 (Ownership Agent)
============================
목적: 기업의 지분 구조를 파악해 매칭 엔진이 맥락 플래그로 쓸 수 있게 한다.

투자 관점에서 지분 구조가 중요한 이유:
  - 실질 유통물량이 적으면 같은 수급에도 주가가 크게 움직인다.
    "외국인 3일 연속 순매수"가 유통 60%인 종목과 24%인 종목에서 의미가 다르다.
  - 오너 지분율은 경영권 안정성과 증자·매각 리스크를 가늠하는 기준이 된다.
  - 승계가 남아 있으면 향후 증여·상속 이벤트 가능성이 있다.

⚠️ 해석에 대한 주의
  "증여 전에는 주가를 누르고 증여 후에 실적이 좋아진다"는 통념은 검증된 법칙이 아니다.
  맞은 사례만 기억되는 편향이 크고, 증여 후에도 주가가 부진한 사례가 흔하다.
  따라서 이 담당자의 출력은 '판정 근거'가 아니라 '배경 정보'로만 쓴다.
  매칭 엔진에서도 점수에 반영하지 않고 플래그로만 표시한다.

데이터 출처 (모두 DART 공식 API):
  - hyslrSttus      최대주주 및 특수관계인 현황 (분기)
  - stockTotqySttus 주식의 총수 현황 — 발행주식총수, 자기주식수 (분기)
  - exctvSttus      임원 현황 — 생년월일, 최대주주와의 관계 (분기)

분기 단위로만 갱신되는 정보이므로 캐시를 길게(30일) 잡는다.
"""

import os
import re
import json
import time
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))

# ── 설정 ──────────────────────────────────────────────
LOOKBACK_DAYS_FOR_MENTIONS = 14   # 피드에서 며칠치 언급 종목을 대상으로 할지
CACHE_TTL_DAYS = 30               # 분기 공시라 한 달 캐시면 충분
DEBUG_FIRST_N = 2                 # 처음 N개 종목은 원본 응답 구조를 출력

# 플래그 기준
FLOAT_SCARCE_THRESHOLD = 30.0     # 실질 유통비율이 이 % 미만이면 '유통물량 희소'
OWNER_WEAK_THRESHOLD = 20.0       # 오너 지분율이 이 % 미만이면 '오너 지분 취약'
SUCCESSION_LOW_THRESHOLD = 20.0   # 승계율이 이 % 미만이면 '승계 미완료'
OWNER_SENIOR_AGE = 65             # 오너가 이 나이 이상이면 '승계 임박' 판단에 사용

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FEED_SIGNALS_FILE = os.path.join(DATA_DIR, "feed_signals.jsonl")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_name_map.json")
CORP_CODES_FILE = os.path.join(DATA_DIR, "corp_codes.xml")
OWNERSHIP_FILE = os.path.join(DATA_DIR, "ownership.json")

DART_API_KEY = os.environ.get("DART_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 최대주주 현황의 '관계' 필드에서 자녀를 식별하는 표현들.
# DART 표기가 회사마다 달라 여러 형태를 모두 잡는다.
CHILD_RELATIONS = ["자녀", "장남", "차남", "삼남", "장녀", "차녀", "삼녀",
                   "아들", "딸", "자부", "사위"]
SELF_RELATIONS = ["본인", "최대주주"]

# 법인 최대주주를 식별하는 표현 (지주사 구조 판별용)
CORP_MARKERS = ["주식회사", "(주)", "㈜", "홀딩스", "지주", "Co.", "Ltd", "Inc",
                "투자", "파트너스", "캐피탈", "자산운용", "연금", "펀드"]


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
    """'1,234' / '12.34' / '-' / None → 숫자 또는 None"""
    if text is None:
        return None
    s = str(text).replace(",", "").replace("%", "").replace("+", "").strip()
    if s in ("", "-", "N/A", "해당사항없음"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pick(row, *candidates):
    """DART 응답의 필드명이 문서와 다를 수 있어 여러 후보를 순서대로 시도한다."""
    for key in candidates:
        if key in row:
            v = to_number(row[key])
            if v is not None:
                return v
    return None


def is_corporation(name):
    """최대주주 이름이 개인이 아니라 법인인지 판별.

    이름만으로 100% 구분할 수는 없으므로 두 단계로 본다.
      1) 법인 표현이 들어 있으면 법인 ("홀딩스", "(주)", "연금" 등)
      2) 순수 한글 2~3자는 개인으로 본다 (한국인 이름 대부분이 여기 해당)
      3) 그 밖(4자 이상 한글, 영문·숫자 포함)은 법인으로 추정
         → "HD현대", "삼성물산" 같은 사명을 잡기 위함.
    4자 이름의 개인 오너를 법인으로 오판할 수 있으나, 그 경우 승계율 계산을
    건너뛰는 데 그치므로 잘못된 신호를 내는 것보다 안전하다.
    이후 임원 현황에서 같은 이름의 생년월일이 확인되면 개인으로 정정한다.
    """
    if not name:
        return False
    if any(m in name for m in CORP_MARKERS):
        return True
    return not re.fullmatch(r"[가-힣]{2,3}", name.strip())


# ── DART 호출 ─────────────────────────────────────────
def dart_get(endpoint, corp_code, bsns_year, debug=False, label=""):
    """DART 정기보고서 주요정보 API 공통 호출. 최근 연도부터 거슬러 시도한다."""
    url = f"https://opendart.fss.or.kr/api/{endpoint}.json"
    current_year = datetime.now(KST).year

    for year in [bsns_year, bsns_year - 1]:
        try:
            res = requests.get(url, params={
                "crtfc_key": DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": "11011",   # 사업보고서
            }, timeout=30)
            data = res.json()
        except Exception as e:
            if debug:
                print(f"    [진단] {label} {year} 요청 실패: {e}")
            continue

        status = data.get("status")
        if debug:
            print(f"    [진단] {label} {year} status={status} "
                  f"({data.get('message')})")

        if status != "000":
            time.sleep(0.12)
            continue

        rows = data.get("list") or []
        if debug and rows:
            print(f"    [진단] {label} 필드: {list(rows[0].keys())}")
            print(f"    [진단] {label} 첫 행: "
                  f"{json.dumps(rows[0], ensure_ascii=False)[:350]}")
        if rows:
            return rows, year
        time.sleep(0.12)

    return [], None


# ── 1) 최대주주 및 특수관계인 ─────────────────────────
def analyze_shareholders(corp_code, year, debug=False):
    """최대주주 현황 → 오너 지분율, 자녀 지분율, 지주사 여부."""
    rows, used_year = dart_get("hyslrSttus", corp_code, year, debug, "최대주주")
    if not rows:
        return None

    total_rate = 0.0        # 최대주주 + 특수관계인 합계 지분율
    owner_rate = 0.0        # 본인(오너) 지분율
    child_rate = 0.0        # 자녀 지분율 합계
    owner_name = None
    members = []

    for row in rows:
        name = (row.get("nm") or "").strip()
        relate = (row.get("relate") or "").strip()
        # 보통주 기준 기말 지분율을 우선 사용
        rate = pick(row, "trmend_posesn_stock_qota_rt", "bsis_posesn_stock_qota_rt")
        if rate is None:
            continue

        stock_kind = (row.get("stock_knd") or "").strip()
        # 우선주 행은 지분율 합산에서 제외 (의결권 기준이 아니므로)
        if "우선" in stock_kind:
            continue
        # '계' / '합계' 행은 중복 합산을 피하려고 건너뛴다
        if name in ("계", "합계", "소계") or relate in ("계", "합계"):
            continue

        total_rate += rate
        members.append({"성명": name, "관계": relate, "지분율": round(rate, 2)})

        if any(k in relate for k in SELF_RELATIONS) and owner_name is None:
            owner_name, owner_rate = name, rate
        elif any(k in relate for k in CHILD_RELATIONS):
            child_rate += rate

    if not members:
        return None

    # 관계 표기가 없어 본인을 못 찾았으면, 지분율이 가장 높은 사람을 오너로 본다
    if owner_name is None:
        top = max(members, key=lambda m: m["지분율"])
        owner_name, owner_rate = top["성명"], top["지분율"]

    holding_structure = is_corporation(owner_name)

    # 승계율 = 자녀 지분 ÷ (본인 + 자녀 지분)
    succession_rate = None
    if not holding_structure and (owner_rate + child_rate) > 0:
        succession_rate = round(child_rate / (owner_rate + child_rate) * 100, 1)

    return {
        "기준연도": used_year,
        "최대주주등_합계_지분율": round(total_rate, 2),
        "오너명": owner_name,
        "오너_지분율": round(owner_rate, 2),
        "자녀_지분율": round(child_rate, 2),
        "승계율": succession_rate,
        "지주사구조": holding_structure,
        "구성원수": len(members),
        "구성원": members[:12],
    }


# ── 2) 주식 총수 / 자기주식 ───────────────────────────
def analyze_stock_total(corp_code, year, debug=False):
    """주식의 총수 현황 → 발행주식총수, 자기주식수."""
    rows, used_year = dart_get("stockTotqySttus", corp_code, year, debug, "주식총수")
    if not rows:
        return None

    issued = None
    treasury = None

    for row in rows:
        se = (row.get("se") or "").strip()
        if "우선" in se:
            continue
        if "보통" in se or "합계" in se:
            v = pick(row, "istc_totqy", "distb_stock_co", "isu_stock_totqy")
            t = pick(row, "tesstk_co")
            if v and v > 1000 and issued is None:
                issued = v
            if t is not None and treasury is None:
                treasury = t
            if "보통" in se and issued:
                break   # 보통주 행을 찾았으면 그것을 우선

    if not issued:
        return None

    return {
        "기준연도": used_year,
        "발행주식총수": int(issued),
        "자기주식수": int(treasury) if treasury else 0,
        "자기주식비율": round((treasury or 0) / issued * 100, 2),
    }


# ── 3) 오너 연령 ──────────────────────────────────────
def find_owner_age(corp_code, year, owner_name, debug=False):
    """임원 현황에서 오너의 생년월일을 찾아 나이를 계산한다.

    DART 임원 현황에는 '최대주주와의 관계' 필드가 있어, 이름이 다르게 적혀 있어도
    본인 여부를 확인할 수 있다.
    """
    rows, _ = dart_get("exctvSttus", corp_code, year, debug, "임원현황")
    if not rows:
        return None, None

    this_year = datetime.now(KST).year
    candidate = None

    for row in rows:
        name = (row.get("nm") or "").strip()
        relate = (row.get("mxmm_shrholdr_relate") or "").strip()
        birth = (row.get("birth_ym") or "").strip()

        is_owner = (owner_name and name == owner_name) or \
                   any(k in relate for k in SELF_RELATIONS)
        if not is_owner or not birth:
            continue

        m = re.search(r"(19\d{2}|20\d{2})", birth)
        if m:
            born = int(m.group(1))
            age = this_year - born
            if 20 < age < 110:
                candidate = (age, (row.get("ofcps") or "").strip())
                break

    if candidate:
        return candidate
    return None, None


# ── 4) 지표 종합 및 플래그 ────────────────────────────
def build_flags(sh, st, owner_age):
    """수치를 사람이 읽는 플래그로 바꾼다."""
    flags = []
    metrics = {}

    # 실질 유통비율 = (발행주식총수 - 최대주주등 - 자기주식) / 발행주식총수
    float_rate = None
    if sh and st:
        locked = (sh["최대주주등_합계_지분율"] or 0) + (st["자기주식비율"] or 0)
        float_rate = round(max(0.0, 100.0 - locked), 2)
        metrics["실질유통비율"] = float_rate
        if float_rate < FLOAT_SCARCE_THRESHOLD:
            flags.append(f"🔒 유통물량 희소 {float_rate}%")

    if sh:
        metrics["오너지분율"] = sh["오너_지분율"]
        metrics["최대주주등합계"] = sh["최대주주등_합계_지분율"]
        metrics["승계율"] = sh["승계율"]
        metrics["지주사구조"] = sh["지주사구조"]

        if sh["지주사구조"]:
            flags.append(f"🏢 지주사 구조 ({sh['오너명']})")
        else:
            if sh["최대주주등_합계_지분율"] < OWNER_WEAK_THRESHOLD:
                flags.append(f"⚠️ 오너 지분 취약 {sh['최대주주등_합계_지분율']}%")
            # 승계 미완료 판단은 오너 연령이 확인될 때만 한다.
            # 나이를 모르면 '아직 승계 안 함'이 의미 있는 신호인지 알 수 없다.
            if (sh["승계율"] is not None
                    and sh["승계율"] < SUCCESSION_LOW_THRESHOLD
                    and owner_age and owner_age >= OWNER_SENIOR_AGE):
                flags.append(
                    f"⏳ 승계 미완료 (자녀 {sh['자녀_지분율']}%, 오너 {owner_age}세)")

    if st and st["자기주식비율"] >= 5:
        flags.append(f"💼 자기주식 {st['자기주식비율']}%")

    if owner_age:
        metrics["오너연령"] = owner_age

    return flags, metrics


# ── 대상 종목 수집 ────────────────────────────────────
def get_target_tickers(ticker_map):
    """피드 담당자가 최근 언급한 종목의 티커 목록."""
    if not os.path.exists(FEED_SIGNALS_FILE):
        print("[알림] feed_signals.jsonl 이 없습니다.")
        return {}

    import xml.etree.ElementTree as ET

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS_FOR_MENTIONS)
    names = set()
    with open(FEED_SIGNALS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if datetime.fromisoformat(rec["timestamp"]) >= cutoff:
                    n = (rec.get("종목명") or "").strip()
                    if n:
                        names.add(n)
            except Exception:
                continue

    result = {}
    for n in names:
        t = ticker_map.get(n)
        if t:
            result[t] = n
    return result


def build_corp_map():
    """corp_codes.xml → {티커: 기업코드}"""
    import xml.etree.ElementTree as ET
    if not os.path.exists(CORP_CODES_FILE):
        print("[오류] corp_codes.xml 이 없습니다.")
        return {}
    try:
        root = ET.parse(CORP_CODES_FILE).getroot()
    except Exception as e:
        print(f"[오류] corp_codes.xml 파싱 실패: {e}")
        return {}
    m = {}
    for item in root.iter("list"):
        c = item.find("corp_code")
        s = item.find("stock_code")
        if c is None or s is None:
            continue
        sc = (s.text or "").strip()
        cc = (c.text or "").strip()
        if len(sc) == 6 and cc:
            m[sc] = cc
    return m


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[텔레그램 미설정] 전송 생략")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(message), 3800):
        try:
            requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": message[i:i + 3800],
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=20)
            time.sleep(0.3)
        except Exception as e:
            print(f"[텔레그램 오류] {e}")
            return


# ── 메인 ─────────────────────────────────────────────
def main():
    if not DART_API_KEY:
        print("[오류] DART_API_KEY 환경변수가 없습니다.")
        return

    ticker_map = load_json(TICKER_MAP_FILE, {})
    if not ticker_map:
        print("[오류] ticker_name_map.json 이 비어 있습니다.")
        return

    targets = get_target_tickers(ticker_map)
    print(f"지분 분석 대상: {len(targets)}종목")
    if not targets:
        return

    corp_map = build_corp_map()
    store = load_json(OWNERSHIP_FILE, {})
    # 사업보고서는 이듬해 3월 공시 → 작년 기준으로 조회
    base_year = datetime.now(KST).year - 1

    updated = 0
    skipped = 0
    processed = 0

    for ticker, name in sorted(targets.items(), key=lambda kv: kv[1]):
        corp_code = corp_map.get(ticker)
        if not corp_code:
            continue

        cached = store.get(ticker)
        if cached:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["_갱신"])
                if age < timedelta(days=CACHE_TTL_DAYS):
                    skipped += 1
                    continue
            except Exception:
                pass

        debug = processed < DEBUG_FIRST_N
        processed += 1
        print(f"\n▶ {name} ({ticker})")

        sh = analyze_shareholders(corp_code, base_year, debug)
        st = analyze_stock_total(corp_code, base_year, debug)
        owner_age, owner_title = (None, None)
        if sh:
            # 지주사로 추정됐더라도 임원 명단에 같은 이름의 생년월일이 있으면
            # 실제로는 개인이므로 정정한다 (이름 기반 판별의 오차 보정).
            owner_age, owner_title = find_owner_age(
                corp_code, base_year, sh["오너명"], debug)
            if sh["지주사구조"] and owner_age:
                sh["지주사구조"] = False
                if (sh["오너_지분율"] + sh["자녀_지분율"]) > 0:
                    sh["승계율"] = round(
                        sh["자녀_지분율"] / (sh["오너_지분율"] + sh["자녀_지분율"]) * 100, 1)
                print(f"  [정정] 임원 명단에서 확인되어 개인 오너로 재분류")

        if not sh and not st:
            print("  지분 정보를 가져오지 못했습니다.")
            continue

        flags, metrics = build_flags(sh, st, owner_age)

        store[ticker] = {
            "_갱신": datetime.now(timezone.utc).isoformat(),
            "종목명": name,
            "플래그": flags,
            "지표": metrics,
            "최대주주": sh,
            "주식총수": st,
            "오너직위": owner_title,
        }
        updated += 1

        summary = " · ".join(flags) if flags else "특이사항 없음"
        float_txt = (f"유통 {metrics['실질유통비율']}%"
                     if "실질유통비율" in metrics else "유통 -")
        owner_txt = (f"오너 {metrics['오너지분율']}%"
                     if "오너지분율" in metrics else "오너 -")
        print(f"  {float_txt} · {owner_txt} · {summary}")
        time.sleep(0.25)

    save_json(OWNERSHIP_FILE, store)

    print(f"\n{'=' * 50}")
    print(f"갱신 {updated}종목 · 캐시 재사용 {skipped}종목 · 저장 {len(store)}종목")

    # 유통물량이 특히 희소한 종목은 따로 알려준다 (수급 해석에 직접 영향)
    scarce = [(v["종목명"], v["지표"]["실질유통비율"])
              for v in store.values()
              if v.get("지표", {}).get("실질유통비율") is not None
              and v["지표"]["실질유통비율"] < FLOAT_SCARCE_THRESHOLD]
    if scarce and updated:
        scarce.sort(key=lambda x: x[1])
        lines = ["🔒 <b>유통물량 희소 종목</b>",
                 "적은 수급에도 주가가 크게 움직일 수 있습니다.\n"]
        for n, r in scarce[:12]:
            lines.append(f"· {n} — 실질 유통 {r}%")
        send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
