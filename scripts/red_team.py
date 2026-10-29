#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
레드팀 — 이 종목을 사지 말아야 할 이유를 모은다.

왜 만드나
  피드가 구조적으로 매수 아이디어 스트림이다. 실측하면 전체 언급 284건 중
  부정이 42건(14.8%)뿐이고, 그 42건조차 3개 종목에 몰려 있다.
  블로거는 안 살 종목을 쓰지 않는다. 그래서 이 엔진을 그냥 두면
  "사라"밖에 말할 수 없다. 반대편을 들 사람이 없으니 기계가 맡는다.

설계 원칙 — 이 셋을 어기면 또 하나의 랭킹 시스템이 된다
  1. **점수를 매기지 않는다.** 위험도 점수를 내면 그걸로 순위를 매기게 되고,
     결국 "덜 위험한 걸 사라"는 또 다른 추천기가 된다. 사실만 나열한다.
  2. **할 말이 없으면 아무 말도 안 한다.** 항상 다섯 개씩 찾아내는 레드팀은
     소음이다. 깨끗하면 '짚을 것 없음'이라고 말한다 — 그것도 정보다.
  3. **항목마다 반증 조건을 붙인다.** 무엇이 바뀌면 이 걱정이 사라지는지
     적지 않으면 영원히 걱정만 하게 된다.

문턱값은 실측으로 정했다 (2026-10-22, 116종목)
  각 검사가 116종목 중 몇 개를 짚는지 세어서, 1~46종목 사이에 들도록 맞췄다.
  결과: 종목당 평균 2.3건 · 짚을 것 없는 종목 7개 · 최대 6건.
  🔴 문턱값을 바꿀 때는 반드시 전 종목에 돌려서 발동 빈도를 다시 세라.
     40% 넘게 발동하는 검사는 경고가 아니라 배경소음이다.
     (처음 쓴 '유효출처 < 1.5' 는 116종목 중 112개를 짚었다. 그래서 버렸다.)

입력 (data/ 아래)
  matching_results.json      오늘 판정                                 [필수]
  valuation_history.json     PER(TTM) 자기역사 — PIT 주간 시계열         [선택]
  feed_signals.jsonl         소스별 커버리지·선행성 → 내러티브 가중치      [선택]
  matching_history.jsonl     판정 안정성                               [선택]

출력
  data/red_team.json

실행
  python scripts/red_team.py              # 전 종목
  python scripts/red_team.py 196170       # 한 종목만 자세히
  python scripts/red_team.py --골든존      # 골든존만
  python scripts/red_team.py --빈도        # 검사별 발동 빈도 (문턱값 점검용)
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("RED_TEAM_DATA", os.path.join(HERE, "..", "data"))
RESULTS = os.path.join(DATA_DIR, "matching_results.json")
VALHIST = os.path.join(DATA_DIR, "valuation_history.json")
FEED = os.path.join(DATA_DIR, "feed_signals.jsonl")
HISTORY = os.path.join(DATA_DIR, "matching_history.jsonl")
OUT = os.path.join(DATA_DIR, "red_team.json")

# ── 문턱값 (괄호 안은 2026-10-22 실측 발동 종목수 / 116) ──────────
MARGIN_PCTILE_LOW = 50.0      # 영업이익률 자기역사 하위 절반      (41)
MARGIN_DECLINE_YEARS = 2      # 영업이익률 연속 하락                ( 7)
DEBT_PCTILE_HIGH = 80.0       # 부채비율 자기역사 상위 구간         (28)
DEBT_HIGH_ABS = 200.0         # 절대 기준 — 자기 比 낮을 때만 별도   ( 7)
GROWTH_SLOWDOWN_PP = 30.0     # 매출성장률 급락 폭                  ( 8)
# 🔴 PER 축은 2026-10-29 에 다시 쟀다. 괄호 안 숫자는 적재 93종목 기준이다.
#    (예전 괄호 (14) 는 엔진의 연간 PER 백분위 기준이었고, 그 경로는 지웠다.)
PER_PCTILE_HIGH = 80.0        # 백분위 80+ 이면 비싼 구간 — 표본 104+ 중 24종목.
                              # 그중 5종목은 현재값이 200 초과라 '무의미' 로 간다.
PER_MIN_SAMPLES = 104         # PIT 2년 — 이보다 짧으면 판정 보류
                              # 상한 제거 후 104 통과: 83 → 87종목
FLOAT_THIN_PCT = 30.0         # 실질유통비율                        ( 5)
OWNER_WEAK_PCT = 20.0         # 최대주주등 합계                     ( 9)
TREASURY_HEAVY_PCT = 10.0     # 자기주식비율                        ( 9)
OWNER_OLD_AGE = 70            # 오너연령 + 승계 미완료              (16)
NEG_TONE_MULT = 2.0           # 부정 비율이 유니버스 평균의 몇 배     ( 3)
NEG_TONE_MIN = 2              # 최소 부정 건수 — 1건은 우연
SCORE_SWING = 20.0            # 2주 내 종합점수 변동폭
MIN_HISTORY_YEARS = 3         # DART 이력                           ( 1)

# 내러티브 소스 가중치 — '표 수'가 아니라 '정보량'으로 센다.
#   커버리지: 많은 종목을 다 언급하는 소스의 한 표는 정보가 적다.
#   선행성:   늘 남 뒤에 언급하는 소스의 한 표는 새 정보가 없다.
# 실측(2026-10-22, 30일 창 631건 / 19소스):
#   pokara61 커버리지 55%(242종목 중 133) → 선행비율 69%여도 가중치 0.23
#   onejejuwave 선행 0 / 추종 16 → 0.30 · doctordk 표본부족 → 1.00
# 🔴 절대 문턱(유효출처 N표 미만)은 쓰지 않는다. 피드 전체가 얇아서
#    중앙값이 0.30이다. 절대 기준을 쓰면 전부 걸린다.
#    대신 '가중치 하위 3분의 1'을 저정보 소스로 보고, 그들만으로
#    골든존의 2곳 조건이 채워졌는지를 묻는다 — 상대 기준이라 자동 보정된다.
COV_LOW, COV_HIGH, COV_W_MIN = 0.10, 0.40, 0.30
LEAD_W_MIN, LEAD_W_MAX, LEAD_UNKNOWN = 0.30, 1.00, 0.65
LEAD_MIN_SAMPLES = 3
WEIGHT_FLOOR = 0.20
FEED_WINDOW_DAYS = 30

# 지적을 보여줄 순서. 투자 판단을 가장 크게 바꾸는 것부터.
# 아이콘 심각도 순이 아니다 — '적자'가 '유통물량 얇음'보다 먼저다.
AXIS_ORDER = ["적자", "수익성", "밸류", "재무", "성장", "지분", "내러티브", "수급", "신뢰도"]


def log(m=""):
    print(m, flush=True)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                      # noqa: BLE001
        return default


def num(v):
    """숫자면 float, 아니면 None. null·문자열·bool 전부 걸러낸다."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def years_of(d) -> list[tuple[int, float]]:
    """{"2014": 29.8, ...} → [(2014, 29.8), ...] 연도 오름차순."""
    out = []
    for k, v in (d or {}).items():
        try:
            y = int(str(k)[:4])
        except (TypeError, ValueError):
            continue
        f = num(v)
        if f is not None and y >= 1990:
            out.append((y, f))
    return sorted(out)


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def consecutive_tail(ys, n):
    """끝에서 n개를 돌려주되, 연도가 실제로 연속일 때만.

    🔴 이게 없으면 "전년 대비"가 거짓말이 된다.
       효성중공업 매출성장률은 {2019: 73.4, 2025: 21.9} 두 해만 있었는데
       ys[-2], ys[-1] 로 비교해서 '전년 대비 51%p 둔화'라고 썼다.
       6년 차이를 전년이라고 부른 것이다. DART 계열에는 빈 해가 있다.
    """
    if len(ys) < n:
        return None
    tail = ys[-n:]
    for i in range(len(tail) - 1):
        if tail[i + 1][0] != tail[i][0] + 1:
            return None
    return tail


def finding(axis, claim, evidence, falsify):
    """지적 한 건. 주장·근거·반증조건이 늘 함께 간다."""
    return {"축": axis, "주장": claim, "근거": evidence, "반증조건": falsify}


# ── 소스 가중치 ──────────────────────────────────────
def source_weights(path=FEED, window_days=FEED_WINDOW_DAYS):
    """소스별 가중치 · 상세 · 저정보 문턱(하위 3분의 1)."""
    recs = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return {}, {}, None
    if not recs:
        return {}, {}, None

    # 창은 '오늘'이 아니라 '마지막 기록일' 기준이다. 피드가 며칠 멈춰도
    # 창이 비어버리지 않게.
    stamps = sorted(str(r.get("timestamp", ""))[:10] for r in recs if r.get("timestamp"))
    if not stamps:
        return {}, {}, None
    last = datetime.strptime(stamps[-1], "%Y-%m-%d")
    cut = (last - timedelta(days=window_days)).strftime("%Y-%m-%d")
    win = [r for r in recs if str(r.get("timestamp", ""))[:10] >= cut] or recs

    stocks_by_src, all_stocks = defaultdict(set), set()
    for r in win:
        nm, src = r.get("종목명"), r.get("source_name")
        if nm and src:
            stocks_by_src[src].add(nm)
            all_stocks.add(nm)
    total = max(1, len(all_stocks))

    first_by_stock, seen_by_stock = {}, defaultdict(set)
    for r in sorted(win, key=lambda x: str(x.get("timestamp", ""))):
        nm, src = r.get("종목명"), r.get("source_name")
        if not nm or not src:
            continue
        seen_by_stock[nm].add(src)
        first_by_stock.setdefault(nm, src)
    lead, follow = defaultdict(int), defaultdict(int)
    for nm, srcs in seen_by_stock.items():
        if len(srcs) < 2:                 # 혼자 언급한 종목은 선행을 따질 수 없다
            continue
        for s in srcs:
            (lead if s == first_by_stock[nm] else follow)[s] += 1

    weights, detail = {}, {}
    for src, stocks in stocks_by_src.items():
        cov = len(stocks) / total
        if cov <= COV_LOW:
            cov_w = 1.0
        elif cov >= COV_HIGH:
            cov_w = COV_W_MIN
        else:
            t = (cov - COV_LOW) / (COV_HIGH - COV_LOW)
            cov_w = 1.0 - t * (1.0 - COV_W_MIN)

        n = lead[src] + follow[src]
        if n < LEAD_MIN_SAMPLES:
            lead_w, ratio = LEAD_UNKNOWN, None
        else:
            ratio = lead[src] / n
            lead_w = LEAD_W_MIN + ratio * (LEAD_W_MAX - LEAD_W_MIN)

        w = max(WEIGHT_FLOOR, round(cov_w * lead_w, 3))
        weights[src] = w
        detail[src] = {
            "가중치": w, "언급건수": sum(1 for r in win if r.get("source_name") == src),
            "종목수": len(stocks), "커버리지": round(cov * 100, 1),
            "선행": lead[src], "추종": follow[src],
            "선행비율": round(ratio * 100) if ratio is not None else None,
        }

    vals = sorted(weights.values())
    low = vals[len(vals) // 3] if vals else None          # 하위 3분의 1 경계
    return weights, detail, low


def universe_negative_rate(rows) -> float | None:
    neg = tot = 0
    for r in rows:
        t = (r.get("내러티브") or {}).get("논조") or {}
        if not isinstance(t, dict):
            continue
        neg += int(num(t.get("부정")) or 0)
        tot += sum(int(num(t.get(k)) or 0) for k in ("긍정", "중립", "부정"))
    return (neg / tot) if tot else None


# ── PER 자기역사 (valuation_history.json 직접 읽기) ───────
class ValHist:
    """{티커: {관측수, 열:[asof,per_fy0,per_ttm,pbr,cap_krw], series:[[...]]}}

    valuation_history.py 를 import 하지 않는다. 그 모듈의 반환 형태에
    의존하면 한쪽을 고칠 때 다른 쪽이 조용히 틀린다. 파일만 읽는다.
    """

    # 🔴 상한을 두지 않는다. 2026-10-29 실측으로 고친 것이다.
    #    예전 규칙은 0.1~200 밖을 버렸는데, 저장된 per_ttm 31,453행 중
    #    1,040행(35종목)이 200 초과였고 전부 **현재값보다 위**에 있었다.
    #    그걸 버리면 남은 분포가 아래쪽으로 몰려 오늘이 더 비싸게 보인다 —
    #    93종목 중 31종목의 백분위가 부풀려져 있었다.
    #      두산    114표본 81.6% → 153표본 60.8% (-20.8%p) ← 거짓 경고였다
    #      삼천당제약 33표본 90.9% → 101표본 44.6% (-46.3%p)
    #    백분위는 순위 기반이라 이상치가 분포를 왜곡하지 않는다. 깎을 이유가
    #    애초에 없었다. 평균·중앙값을 쓸 때나 이상치를 걱정하는 것이다.
    #    0 이하·극소값만 데이터 오류로 막는다.
    FLOOR = 0.1

    # 현재값이 이보다 크면 백분위는 내되 '이익이 미미해 PER 이 뜻을 잃는 구간'
    # 이라고 함께 말한다. PER 4,929배(드림텍)는 비싸다기보다 분모가 0에 가깝다는
    # 뜻이다. 예전 규칙은 이런 종목을 통째로 버려서 가장 비싼 구간을 침묵했다 —
    # 주성엔지니어링 99.7% · 삼성SDI 99.3% · 드림텍 98.9% 가 '판정 보류'였다.
    MEANINGLESS = 200.0

    def __init__(self, raw):
        self.raw = raw or {}

    @classmethod
    def load(cls, path=VALHIST):
        return cls(load_json(path, {}))

    def ttm_position(self, tk):
        e = self.raw.get(tk)
        if not isinstance(e, dict):
            return None
        cols, series = e.get("열") or [], e.get("series") or []
        if "per_ttm" not in cols or not series:
            return None
        i = cols.index("per_ttm")
        vals = []
        for row in series:
            if not isinstance(row, list) or len(row) <= i:
                continue
            v = num(row[i])
            if v is not None and v >= self.FLOOR:
                vals.append(v)
        if not vals:
            return None
        cur = vals[-1]
        below = sum(1 for v in vals if v <= cur)
        return {
            "현재": round(cur, 1), "표본": len(vals),
            "백분위": round(below / len(vals) * 100, 1),
            "중앙값": round(median(vals), 1),
            "충분": len(vals) >= PER_MIN_SAMPLES,
            "무의미": cur > self.MEANINGLESS,
        }


# ── 개별 점검 ────────────────────────────────────────
def check_profitability(F, out):
    cur = num((F.get("현재") or {}).get("영업이익률"))
    pct = num((F.get("백분위") or {}).get("영업이익률"))
    ys = years_of((F.get("연도별") or {}).get("영업이익률"))

    if cur is not None and cur <= 0:
        out.append(finding(
            "적자", "지금 영업적자입니다.",
            f"최근 영업이익률 {cur:.1f}%",
            "흑자 전환하면 해소됩니다. 그 전까지 PER·ROE 는 뜻이 없습니다."))
        return                                   # 적자면 아래 비교는 의미 없다

    if pct is not None and pct < MARGIN_PCTILE_LOW:
        med = median([v for _, v in ys]) if ys else None
        ev = f"자기 {len(ys)}년 이력에서 하위 {pct:.0f}% 구간"
        if med is not None and cur is not None:
            ev += f" · 최근 {cur:.1f}% vs 중앙값 {med:.1f}%"
        out.append(finding(
            "수익성", "영업이익률이 자기 평균 아래입니다.",
            ev,
            "다음 연간 실적이 중앙값 위로 올라오면 해소됩니다."))

    tail = consecutive_tail(ys, MARGIN_DECLINE_YEARS + 1)
    if tail and all(tail[i][1] > tail[i + 1][1] for i in range(len(tail) - 1)):
        out.append(finding(
            "수익성", f"영업이익률이 {MARGIN_DECLINE_YEARS}년 연속 내려왔습니다.",
            " → ".join(f"{y} {v:.1f}%" for y, v in tail),
            "한 해라도 반등하면 추세가 깨집니다."))


def check_valuation(tk, F, vh, out):
    """주간 PIT TTM 자기역사만 쓴다.

    🔴 엔진의 연간 PER 백분위는 쓰지 않는다. 실측하면 PER이력연수가
       최대 3년이다 (0년 20 · 1년 19 · 2년 24 · 3년 53 / 116종목).
       점 3개로 '상위 20%'를 말하는 것은 근거가 아니라 숫자 장식이다.
       3년 미만을 경고로 올리면 54%가 걸려서 배경소음이 된다.
       그래서 연간 이력으로는 '비쌈'을 판정하지 않고, 침묵한다.
       주간 PIT 적재(valuation_history.json)가 있는 이유가 이것이다.

    🔴 두 PER 을 같은 줄에 섞지 않는다. 정의가 다르다 —
       엔진의 PER 은 연간 확정 기준이고, PIT 는 주간 TTM 기준이다.
    """
    pos = vh.ttm_position(tk) if vh else None

    if pos and pos["충분"]:
        # 이익이 거의 없어 PER 이 뜻을 잃은 구간. 백분위보다 이 사실이 먼저다.
        # 자기역사 상위권이 아니어도 말해야 한다 — 로보티즈 966배는 백분위
        # 65%지만 분모가 0에 가깝다는 사실 자체가 지적거리다.
        if pos["무의미"]:
            out.append(finding(
                "밸류", "이익이 미미해 PER 이 뜻을 잃는 구간입니다.",
                f"PER(TTM) {pos['현재']:,.0f}배 · 주간 {pos['표본']}개 표본 중 "
                f"상위 {100 - pos['백분위']:.0f}% (중앙값 {pos['중앙값']})",
                "분모가 0에 가까워서 배수가 커진 것이라, 비싸다·싸다로 읽으면 "
                "안 됩니다. 이익이 정상 수준으로 돌아오면 판정이 가능해집니다."))
        elif pos["백분위"] >= PER_PCTILE_HIGH:
            out.append(finding(
                "밸류", "자기역사에서 비싼 구간입니다.",
                f"PER(TTM) {pos['현재']} · 주간 {pos['표본']}개 표본 중 "
                f"상위 {100 - pos['백분위']:.0f}% (중앙값 {pos['중앙값']})",
                "이익이 늘어 분모가 커지면 백분위가 내려옵니다. "
                "주가가 빠져도 내려오지만 그건 손실입니다."))
        return                                   # 충분하면 여기서 끝. 침묵도 결론이다.

    # 판정 보류 — 왜 보류인지가 정보다. 가진 것 중 더 나은 쪽을 말한다.
    per_yrs = num(F.get("PER이력연수"))
    excl = F.get("PER제외연도") or {}
    tail = ""
    if isinstance(excl, dict) and excl:
        tail = " · 연간 PER 은 " + ", ".join(
            f"{y}년 {num(v):.0f}" for y, v in sorted(excl.items())[-3:]
            if num(v) is not None) + " 로 제외됨"
    elif per_yrs is not None:
        tail = f" · 연간 이력도 {per_yrs:.0f}년뿐"

    if pos:
        ev = f"주간 표본 {pos['표본']}개 (최소 {PER_MIN_SAMPLES}개 필요){tail}"
        falsify = f"적재가 {PER_MIN_SAMPLES}주를 넘기면 판정 가능합니다."
    else:
        ev = f"주간 밸류 적재에 이 종목이 없습니다{tail}"
        falsify = "적재 대상에 넣으면 2년 뒤부터 판정 가능합니다."

    out.append(finding(
        "밸류", "비싼지 싼지 판단을 보류합니다.", ev,
        falsify + " 그 전까지 'PER 몇 배' 는 비교 대상이 없는 숫자입니다."))


def check_debt(F, out, is_financial=False):
    cur = num((F.get("현재") or {}).get("부채비율"))
    pct = num((F.get("백분위") or {}).get("부채비율"))
    ys = years_of((F.get("연도별") or {}).get("부채비율"))
    if cur is None:
        return
    med = median([v for _, v in ys]) if ys else None
    note = " (금융업은 절대 기준이 맞지 않습니다)" if is_financial else ""

    if pct is not None and pct >= DEBT_PCTILE_HIGH:
        ev = f"최근 {cur:.0f}% · 자기 {len(ys)}년 이력에서 상위 {100 - pct:.0f}%"
        if med is not None:
            ev += f" (중앙값 {med:.0f}%)"
        out.append(finding(
            "재무", "부채비율이 자기역사에서 높은 구간입니다.",
            ev + note,
            "차입 상환이나 자본 확충으로 중앙값 부근까지 내려오면 해소됩니다."))
        return

    # 절대로는 높지만 자기 과거보다는 낮은 경우. 경고를 지우지 말고 맥락을 붙인다.
    # 예: 코스맥스 247% — 높지만 12년 중앙값 294% 보다 낮다. 업종 특성일 수 있다.
    if cur >= DEBT_HIGH_ABS and not is_financial:
        ev = f"최근 {cur:.0f}%"
        if med is not None:
            ev += f" · 다만 자기 {len(ys)}년 중앙값 {med:.0f}% 보다는 낮습니다"
        out.append(finding(
            "재무", "부채비율이 절대 수준으로 높습니다.",
            ev,
            "자기 과거보다 낮으니 업종 특성일 수 있습니다. "
            "동업종과 비교해 보셔야 합니다."))


def check_growth(F, out):
    cur = num((F.get("현재") or {}).get("매출성장률"))
    ys = years_of((F.get("연도별") or {}).get("매출성장률"))
    pair = consecutive_tail(ys, 2)        # 빈 해를 건너뛰어 비교하면 안 된다
    if cur is not None and cur < 0:
        out.append(finding(
            "성장", "매출이 역성장했습니다.",
            f"최근 매출성장률 {cur:.1f}%"
            + (f" · 전년 {pair[0][1]:+.1f}%" if pair else ""),
            "다음 해 매출이 늘면 해소됩니다."))
        return
    if pair:
        (py, prev), (ny, now) = pair
        if prev - now >= GROWTH_SLOWDOWN_PP:
            out.append(finding(
                "성장", "매출 성장이 급격히 꺾였습니다.",
                f"{py} {prev:+.1f}% → {ny} {now:+.1f}% ({prev - now:.0f}%p 둔화)",
                "둔화가 멈추면 해소됩니다. 전년 기저가 높았던 것인지 "
                "확인이 필요합니다."))


def check_ownership(Z, out):
    fl = num(Z.get("실질유통비율"))
    if fl is not None and fl < FLOAT_THIN_PCT:
        out.append(finding(
            "지분", "유통물량이 얇습니다.",
            f"실질유통비율 {fl:.1f}%",
            "변동성이 양방향으로 커집니다. 물량이 풀리면 해소되지만 "
            "그 자체가 수급 부담입니다."))

    own = num(Z.get("최대주주등합계"))
    if own is not None and 0 < own < OWNER_WEAK_PCT:
        out.append(finding(
            "지분", "최대주주 지분이 취약합니다.",
            f"최대주주등 합계 {own:.1f}%"
            + (f" (최대주주 {Z['최대주주명']})" if Z.get("최대주주명") else ""),
            "경영권 분쟁이나 적대적 인수 여지가 있습니다. "
            "지분을 늘리면 해소됩니다."))

    tre = num(Z.get("자기주식비율"))
    if tre is not None and tre >= TREASURY_HEAVY_PCT:
        out.append(finding(
            "지분", "자사주가 많습니다.",
            f"자기주식 {tre:.1f}%",
            "소각하면 주주환원이지만 처분하면 물량 부담입니다. "
            "소각 공시가 나오면 해소됩니다."))

    succ, age = num(Z.get("승계율")), num(Z.get("오너연령"))
    if age is not None and age >= OWNER_OLD_AGE and (succ is None or succ < 1):
        out.append(finding(
            "지분", "승계가 끝나지 않았습니다.",
            f"오너 {age:.0f}세 · 자녀 지분 "
            + (f"{succ:.1f}%" if succ is not None else "확인 안 됨"),
            "상속·증여 재원 마련으로 지분이 시장에 나올 수 있습니다. "
            "승계가 진행되면 해소됩니다."))


def check_narrative(Nr, weights, low_w, neg_base, out):
    srcs = [s for s in (Nr.get("출처목록") or []) if s]
    if srcs and weights and low_w is not None:
        w = {s: weights.get(s, LEAD_UNKNOWN) for s in srcs}
        weak = [s for s in srcs if w[s] <= low_w]
        shown = " · ".join(f"@{s} {w[s]:.2f}표" for s in sorted(srcs, key=lambda s: w[s]))

        if 2 <= len(srcs) <= 3 and len(weak) == len(srcs):
            out.append(finding(
                "내러티브", f"언급한 {len(srcs)}곳이 모두 저정보 소스입니다.",
                f"{shown} · 유효 {sum(w.values()):.2f}표 "
                f"(저정보 기준 {low_w:.2f}표 이하)",
                "커버리지가 좁은 소스나 먼저 짚는 소스가 언급하면 해소됩니다. "
                f"출처가 {len(srcs)}곳이라는 사실만으로 독립 확인이 되지는 않습니다."))
        elif len(srcs) >= 4 and len(weak) == len(srcs):
            # 넷 이상이 전부 저정보일 때 '모두 저정보'라고만 쓰면 과장이 된다.
            # 곳 수는 많으니, 과장 없이 유효 표만 말한다.
            out.append(finding(
                "내러티브", f"출처 {len(srcs)}곳이지만 유효 "
                            f"{sum(w.values()):.1f}표에 그칩니다.",
                f"{shown} · 모두 커버리지가 넓거나 추종 성향입니다",
                "곳 수가 아니라 서로 독립인지가 중요합니다. 먼저 짚는 소스가 "
                "언급하면 유효 표가 올라갑니다."))
        elif len(srcs) == 2 and weak:
            out.append(finding(
                "내러티브", f"@{weak[0]} 를 빼면 출처가 1곳으로 줄어듭니다.",
                f"{shown} · 골든존 조건은 2곳입니다",
                "세 번째 소스가 독립적으로 언급하면 해소됩니다."))

    t = Nr.get("논조")
    if isinstance(t, dict) and neg_base:
        neg = int(num(t.get("부정")) or 0)
        tot = sum(int(num(t.get(k)) or 0) for k in ("긍정", "중립", "부정"))
        if tot >= 3 and neg >= NEG_TONE_MIN and neg / tot >= neg_base * NEG_TONE_MULT:
            out.append(finding(
                "내러티브", "부정적 언급 비율이 유니버스 평균보다 높습니다.",
                f"부정 {neg}/{tot}건 ({neg / tot * 100:.0f}%) · "
                f"유니버스 평균 {neg_base * 100:.1f}%",
                "피드에서 부정은 드뭅니다. 원문을 직접 읽어 보셔야 합니다 — "
                "무엇을 걱정하는지가 핵심입니다."))


def check_supply(signals, out):
    """수급신호 문구에서 '평소의 N배'를 뽑는다.

    예: "외국인 9일 연속 순매수 (최근일 25.1억원, 평소의 0.2배)"
    N < 1 이면 연속일수는 늘어나는데 규모는 평소보다 작다는 뜻이다.
    실측 46/116 종목이 여기 걸린다 — 경고가 아니라 맥락으로 읽어야 한다.
    """
    joined = " / ".join(str(s) for s in (signals or []))
    if not joined:
        return
    mults = [float(m) for m in re.findall(r"평소의\s*([\d.]+)\s*배", joined)]
    if mults and min(mults) < 1.0:
        out.append(finding(
            "수급", "연속 순매수지만 규모는 평소보다 작습니다.",
            f"{joined} · 연속일수는 쌓이는데 금액은 평소의 {min(mults):.1f}배",
            "급증 신호가 함께 뜨면 해소됩니다. 금액이 작은 날은 "
            "KRX 단일시장과 KRX+NXT 통합 사이에서 부호까지 뒤바뀔 수 있습니다."))


def check_reliability(F, swing, out):
    yrs = num(F.get("이력연수"))
    if yrs is not None and yrs < MIN_HISTORY_YEARS:
        out.append(finding(
            "신뢰도", "재무 이력이 짧습니다.",
            f"DART 이력 {yrs:.0f}년 (최소 {MIN_HISTORY_YEARS}년 권장)",
            "연차가 쌓이면 자기 과거 대비 판정이 가능해집니다."))
    if swing is not None and abs(swing) >= SCORE_SWING:
        out.append(finding(
            "신뢰도", "최근 판정이 크게 흔들렸습니다.",
            f"2주 사이 종합점수 {swing:+.1f}점 변동",
            "대부분 실적이 아니라 언급 수 변화입니다. 펀더멘탈 점수가 "
            "같이 움직였는지 확인해 보셔야 합니다."))


# ── 조립 ─────────────────────────────────────────────
FINANCIAL = ("은행", "증권", "보험", "캐피탈", "금융지주", "카드", "저축은행")


def is_financial(name) -> bool:
    """matching_results 에 업종 필드가 없어서 이름으로 가린다. 거친 방법이다."""
    return any(k in str(name) for k in FINANCIAL)


def score_swings(path=HISTORY, days=14):
    """종목별 최근 2주 종합점수 변동폭. (날짜,티커) 중복은 최신 timestamp 채택."""
    best = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d, tk = r.get("날짜"), r.get("티커")
                if not d or not tk:
                    continue
                prev = best.get((d, tk))
                if prev is None or str(r.get("timestamp", "")) > str(prev.get("timestamp", "")):
                    best[(d, tk)] = r
    except FileNotFoundError:
        return {}
    by_tk = defaultdict(list)
    for (d, tk), r in best.items():
        s = num(r.get("종합점수"))
        if s is not None:
            by_tk[tk].append((d, s))
    out = {}
    for tk, rws in by_tk.items():
        rws.sort()
        if len(rws) < 2:
            continue
        try:
            cut = (datetime.strptime(rws[-1][0], "%Y-%m-%d")
                   - timedelta(days=days)).strftime("%Y-%m-%d")
        except ValueError:
            continue
        older = [v for d, v in rws if d <= cut] or [rws[0][1]]
        out[tk] = rws[-1][1] - older[-1]
    return out


def analyse(row, vh, weights, low_w, neg_base, swing):
    F = row.get("펀더멘탈") or {}
    Z = row.get("지분지표") or {}
    Nr = row.get("내러티브") or {}
    tk = row.get("티커")

    out = []
    check_profitability(F, out)
    check_valuation(tk, F, vh, out)
    check_debt(F, out, is_financial(row.get("종목명")))
    check_growth(F, out)
    check_ownership(Z, out)
    check_narrative(Nr, weights, low_w, neg_base, out)
    check_supply(row.get("수급신호"), out)
    check_reliability(F, swing.get(tk), out)

    out.sort(key=lambda x: AXIS_ORDER.index(x["축"]) if x["축"] in AXIS_ORDER else 99)
    return out


# ── 주간리포트 연결 ──────────────────────────────────
# weekly_report.py 를 거의 안 건드리게, 렌더링까지 여기서 한다.
# 리포트에 넣을 코드는 세 줄이다:
#
#   from red_team import load_findings, render_block      # 파일 상단
#   red = load_findings()                                 # build_payload() 안
#   ... 카드 HTML 뒤에 ... render_block(red, 티커)         # 카드 렌더 안
#
# CSS 도 블록 안에 인라인으로 넣는다 — 스타일시트를 고칠 필요가 없다.
# 🔴 색을 칠하지 않는다. 빨간 배너를 붙이면 '위험 등급'처럼 읽히고,
#    그러면 또 하나의 랭킹이 된다. 회색 테두리와 글자만 쓴다.

def load_findings(path=OUT):
    """red_team.json 을 읽는다. 없으면 빈 dict — 리포트는 그대로 돌아간다."""
    d = load_json(path, {}) or {}
    return {k: v for k, v in d.items() if not str(k).startswith("_")}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_block(findings, ticker, max_items=5) -> str:
    """한 종목의 반대 증거 블록. 짚을 것이 없으면 그렇다고 한 줄 쓴다.

    없을 때 빈 문자열을 돌려주지 않는 이유: '검사했고 깨끗하다'와
    '검사를 안 했다'는 다른 정보다. 카드에서 그 둘이 구분돼야 한다.
    """
    p = (findings or {}).get(str(ticker))
    if p is None:
        return ""                                  # 이 종목은 아예 대상이 아니었다

    s = ("border:1px solid #d8d8d8;border-radius:6px;padding:10px 12px;"
         "margin:10px 0 2px;font-size:12px;line-height:1.55;color:#3c3c3b")
    head = ('<div style="font-weight:700;color:#595959;letter-spacing:-.2px;'
            'margin-bottom:6px">사지 말아야 할 이유</div>')

    items = p.get("지적") or []
    if not items:
        return (f'<div style="{s}">{head}'
                '<div style="color:#595959">검사한 9개 축에서 짚을 것이 없었습니다. '
                '검사가 닿지 않는 위험은 여전히 있습니다.</div></div>')

    rows = []
    for x in items[:max_items]:
        rows.append(
            '<div style="margin:0 0 8px;padding-left:9px;'
            'border-left:2px solid #bfbfbf">'
            f'<span style="color:#595959;font-size:11px">{_esc(x["축"])}</span> '
            f'<b style="font-weight:600">{_esc(x["주장"])}</b>'
            f'<div style="color:#595959">{_esc(x["근거"])}</div>'
            f'<div style="color:#8a8a88;font-size:11px">'
            f'해소 조건 — {_esc(x["반증조건"])}</div></div>')
    more = ""
    if len(items) > max_items:
        more = (f'<div style="color:#8a8a88;font-size:11px">'
                f'그 외 {len(items) - max_items}건</div>')
    return f'<div style="{s}">{head}{"".join(rows)}{more}</div>'


def main(argv):
    flags = {a for a in argv[1:] if a.startswith("--")}
    args = [a for a in argv[1:] if not a.startswith("--")]

    results = load_json(RESULTS)
    if not results:
        log(f"{RESULTS} 를 읽을 수 없습니다. 매칭 엔진을 먼저 실행하세요.")
        return 1
    rows = [r for r in (results.get("결과") or []) if r.get("티커")]
    if not rows:
        log("matching_results.json 의 '결과' 가 비어 있습니다.")
        return 1

    vh = ValHist.load()
    weights, detail, low_w = source_weights()
    neg_base = universe_negative_rate(rows)
    swing = score_swings()

    log(f"레드팀 — {datetime.now(KST):%Y-%m-%d %H:%M} KST · 대상 {len(rows)}종목")
    log(f"  밸류역사 {len(vh.raw)}종목 · 소스 {len(weights)}곳 "
        f"(저정보 기준 ≤{low_w:.2f}표)" if low_w is not None else
        f"  밸류역사 {len(vh.raw)}종목 · 소스 가중치 없음")
    if neg_base is not None:
        log(f"  유니버스 부정 논조 {neg_base * 100:.1f}% "
            f"→ 부정 문턱 {neg_base * NEG_TONE_MULT * 100:.0f}% 이상 + {NEG_TONE_MIN}건 이상")

    out, counts = {}, defaultdict(int)
    for r in rows:
        tk = r.get("티커")
        found = analyse(r, vh, weights, low_w, neg_base, swing)
        for x in found:
            counts[x["축"] + ":" + x["주장"][:24]] += 1
        out[tk] = {"종목명": r.get("종목명"), "라벨": r.get("라벨"),
                   "지적": found,
                   "요약": f"짚을 것 {len(found)}건" if found else "짚을 것 없음"}

    # ── 한 종목 자세히 ──
    if args:
        tk = args[0]
        p = out.get(tk) or next((v for k, v in out.items() if v["종목명"] == tk), None)
        if not p:
            log(f"\n{tk} 는 대상에 없습니다.")
            return 1
        log(f"\n{p['종목명']} · {p['라벨']} — {p['요약']}")
        for i, x in enumerate(p["지적"], 1):
            log(f"\n  {i}. [{x['축']}] {x['주장']}")
            log(f"     근거: {x['근거']}")
            log(f"     반증: {x['반증조건']}")
        if not p["지적"]:
            log("\n  이 종목에서는 반대 증거를 찾지 못했습니다. "
                "검사가 닿지 않는 위험은 여전히 있습니다.")
        return 0

    # ── 발동 빈도 (문턱값 점검용) ──
    if "--빈도" in flags:
        log(f"\n검사별 발동 빈도 — 40% 넘으면 배경소음이다")
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            bar = "█" * max(1, round(v / len(rows) * 40))
            log(f"  {v:3d}/{len(rows)} ({v / len(rows) * 100:4.1f}%) {bar} {k}")

    # ── 저장 ──
    payload = dict(out)
    payload["_생성"] = datetime.now(KST).isoformat(timespec="seconds")
    payload["_소스가중치"] = detail
    payload["_기준"] = {
        "저정보표문턱": low_w, "유니버스부정률": round(neg_base * 100, 1) if neg_base else None,
        "영업이익률백분위": MARGIN_PCTILE_LOW, "부채백분위": DEBT_PCTILE_HIGH,
        "PER백분위": PER_PCTILE_HIGH, "PER최소표본": PER_MIN_SAMPLES,
        "PER하한": ValHist.FLOOR, "PER무의미기준": ValHist.MEANINGLESS,
        "유통물량": FLOAT_THIN_PCT, "최대주주": OWNER_WEAK_PCT,
    }
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=1)

    stocks = [v for k, v in payload.items() if not k.startswith("_")]
    total = sum(len(v["지적"]) for v in stocks)
    clean = sum(1 for v in stocks if not v["지적"])
    log(f"\n지적 {total}건 · 종목당 평균 {total / len(stocks):.1f}건 · "
        f"짚을 것 없음 {clean}/{len(stocks)}종목")

    gz = [v for v in stocks if "골든존" in str(v["라벨"])]
    if gz:
        log(f"\n골든존 {len(gz)}종목")
        for v in sorted(gz, key=lambda x: -len(x["지적"])):
            axes = " · ".join(f"{x['축']}" for x in v["지적"]) or "짚을 것 없음"
            log(f"  {str(v['종목명'])[:12]:<14s} {len(v['지적'])}건  {axes}")
    log(f"\n  저장: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
