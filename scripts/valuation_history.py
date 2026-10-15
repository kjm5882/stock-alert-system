#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
밸류에이션 자기역사 — 종목별 PER·PBR 과거 분포에서 '지금 어디쯤인가'를 계산.

왜 이 방식인가
  네이버는 PER 과거치를 3년만 준다. 3년 표본으로는 백분위가 흔들린다.
  open-proxy-mcp 의 price_multiple_data(scope=firm_history) 는 주간 PIT
  시계열을 수년치 준다 — 한양이엔지는 342개(2020-04~).

  그런데 그건 MCP 라서 GitHub Actions 에서 호출할 수 없다. 대신
  **과거 분포는 변하지 않는다**는 성질을 쓴다. 대화에서 한 번 받아
  data/valuation_history.json 에 적재해 두고, 엔진은 그 파일만 읽는다.
  주 1회 갱신이면 충분하다 — 주간 데이터이기 때문이다.

🔴 정의 주의
  이 파일의 PER = 보통주 시총 ÷ 지배주주순이익 (PIT).
  네이버 PER = 주가 ÷ EPS. 두 숫자를 섞으면 안 된다.
  그래서 현재값도 이 파일의 마지막 관측을 쓴다 — 네이버 현재가와
  나란히 놓지 않는다.

사용
    from valuation_history import ValuationHistory
    vh = ValuationHistory.load()
    info = vh.position("045100", "per_fy0")
    #  → {"현재": 13.18, "백분위": 95.9, "중앙값": 5.65, "표본": 342, ...}
    print(vh.describe("045100"))
    #  → "PER 13.18 — 자기역사 6.5년(342주) 중 상위 4% (중앙값 5.65)"
"""

from __future__ import annotations

import json
import os
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "..", "data", "valuation_history.json")

# 표본이 이보다 적으면 백분위를 내지 않는다.
# 주간 관측이므로 104 = 약 2년. 3년 표본(158개)에서도 기준선이
# 28% 흔들렸으니, 2년 미만은 '판단보류'가 정직하다.
# 기본 지표는 TTM(최근 4분기 지배순이익) 이다.
# FY0 은 '직전 확정 사업연도' 기준이라 실적이 급변하는 구간에서 딴 얘기를 한다 —
# SK하이닉스가 FY0 으로는 백분위 52%(중간권)인데 TTM 으로는 21.7%(싼 편)였다.
# 매칭 엔진이 보여주는 네이버 PER(6.5)과도 TTM(7.24)이 같은 세계의 숫자다.
DEFAULT_KEY = "per_ttm"

MIN_SAMPLES = 104

# 이 날짜보다 오래된 적재분은 신선하지 않다고 본다(일).
STALE_AFTER_DAYS = 14

VALID_RANGES = {            # 바깥은 이상치로 버린다
    "per_fy0": (0.1, 200.0),
    "per_ttm": (0.1, 200.0),
    "pbr": (0.01, 50.0),
}


class ValuationHistory:
    """적재된 시계열을 읽어 백분위를 내는 읽기 전용 객체."""

    def __init__(self, payload: dict):
        self._meta = {k: v for k, v in payload.items() if k.startswith("_")}
        self._by_ticker = {k: v for k, v in payload.items() if not k.startswith("_")}

    # ── 적재 ────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | None = None) -> "ValuationHistory":
        """파일이 없으면 빈 객체를 돌려준다 — 엔진이 죽지 않게."""
        path = path or DEFAULT_PATH
        if not os.path.exists(path):
            print(f"  [밸류역사] 파일 없음 ({path}) — PER 자기역사 생략")
            return cls({})
        try:
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f))
        except Exception as exc:                          # noqa: BLE001
            print(f"  [밸류역사] 읽기 실패: {str(exc)[:80]} — 생략")
            return cls({})

    # ── 메타 ────────────────────────────────────────────
    @property
    def tickers(self) -> list[str]:
        return sorted(self._by_ticker)

    @property
    def 정의(self) -> str:
        return self._meta.get("_정의", "")

    def staleness(self) -> tuple[int | None, bool]:
        """(적재 후 경과일, 신선한가)."""
        raw = self._meta.get("_생성")
        if not raw:
            return None, False
        try:
            made = datetime.fromisoformat(raw)
        except ValueError:
            return None, False
        if made.tzinfo is None:
            made = made.replace(tzinfo=KST)
        days = (datetime.now(KST) - made).days
        return days, days <= STALE_AFTER_DAYS

    # ── 핵심 ────────────────────────────────────────────
    def series(self, ticker: str, key: str = DEFAULT_KEY) -> list[tuple[str, float]]:
        """(날짜, 값) 오름차순. 이상치와 결측은 버린다."""
        rec = self._by_ticker.get(str(ticker))
        if not rec:
            return []
        cols = rec.get("열") or []
        if "asof" not in cols or key not in cols:
            return []
        i_date, i_val = cols.index("asof"), cols.index(key)
        lo, hi = VALID_RANGES.get(key, (None, None))
        out = []
        for row in rec.get("series") or []:
            if len(row) <= max(i_date, i_val):
                continue
            v = row[i_val]
            if not isinstance(v, (int, float)):
                continue
            if lo is not None and not (lo <= v <= hi):
                continue
            out.append((str(row[i_date]), float(v)))
        out.sort(key=lambda t: t[0])
        return out

    def position(self, ticker: str, key: str = DEFAULT_KEY) -> dict | None:
        """현재값이 자기 과거 분포의 몇 번째 백분위인가.

        백분위는 '현재값보다 낮았던 관측의 비율'이다.
        95.9% = 과거의 95.9% 구간보다 지금이 비싸다.
        """
        s = self.series(ticker, key)
        if len(s) < MIN_SAMPLES:
            return {
                "지표": key,
                "표본": len(s),
                "표본부족": True,
                "필요표본": MIN_SAMPLES,
            } if s else None

        dates = [d for d, _ in s]
        vals = sorted(v for _, v in s)
        cur_date, cur = s[-1]

        below = bisect_left(vals, cur)
        pct = below / len(vals) * 100
        n = len(vals)
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

        years = (int(dates[-1][:4]) - int(dates[0][:4])) + \
                (int(dates[-1][4:6]) - int(dates[0][4:6])) / 12

        return {
            "지표": key,
            "현재": round(cur, 2),
            "기준일": cur_date,
            "백분위": round(pct, 1),
            "중앙값": round(median, 2),
            "최소": round(vals[0], 2),
            "최대": round(vals[-1], 2),
            "표본": n,
            "역사연수": round(years, 1),
            "시작일": dates[0],
            "표본부족": False,
        }

    # ── 적재가 묵어도 현재값을 살리는 길 ────────────────
    def implied_earnings(self, ticker: str) -> float | None:
        """마지막 관측에서 지배주주순이익을 역산한다.

        PER = 시가총액 ÷ 지배순이익 이므로 순이익 = 시총 ÷ PER.
        분모는 분기에 한 번만 바뀌니, 적재가 몇 주 묵어도 이 값은 유효하다.
        """
        rec = self._by_ticker.get(str(ticker))
        if not rec:
            return None
        cols = rec.get("열") or []
        if "cap_krw" not in cols or DEFAULT_KEY not in cols:
            return None          # cap 없이 적재된 옛 파일
        ic, ip = cols.index("cap_krw"), cols.index(DEFAULT_KEY)
        for row in reversed(rec.get("series") or []):
            if len(row) <= max(ic, ip):
                continue
            cap, per = row[ic], row[ip]
            if isinstance(cap, (int, float)) and isinstance(per, (int, float)) and per > 0:
                return cap / per
        return None

    def live_position(self, ticker: str, market_cap) -> dict | None:
        """오늘 시가총액으로 계산한 PER 이 자기 과거 분포의 어디인가.

        엔진이 매일 받는 시가총액을 넣으면, 적재 파일이 묵었더라도
        현재값만은 오늘 것이 된다. 과거 분포는 어차피 변하지 않는다.
        네이버 PER(주가÷EPS)을 섞지 않으므로 정의도 어긋나지 않는다.
        """
        if not isinstance(market_cap, (int, float)) or market_cap <= 0:
            return None
        earn = self.implied_earnings(ticker)
        if not earn or earn <= 0:
            return None           # 적자이거나 역산 불가
        base = self.position(ticker, DEFAULT_KEY)
        if not base or base.get("표본부족"):
            return base

        s = self.series(ticker, DEFAULT_KEY)
        vals = sorted(v for _, v in s)
        cur = market_cap / earn
        pct = bisect_left(vals, cur) / len(vals) * 100

        out = dict(base)
        out.update({"현재": round(cur, 2), "백분위": round(pct, 1),
                    "기준일": "오늘(시총 기준)", "적재현재": base["현재"]})
        return out

    def describe(self, ticker: str, key: str = DEFAULT_KEY) -> str:
        """텔레그램에 그대로 넣을 한 줄. 점수화하지 않고 위치만 말한다."""
        label = {"per_fy0": "PER(FY0)", "per_ttm": "PER(TTM)", "pbr": "PBR"}.get(key, key)
        p = self.position(ticker, key)
        if p is None:
            return f"{label} 자기역사 없음"
        if p["표본부족"]:
            return (f"{label} 판단보류 — 자기역사 {p['표본']}주뿐"
                    f"(최소 {p['필요표본']}주 필요)")

        pctile = p["백분위"]
        if pctile >= 90:
            where = f"상위 {100 - pctile:.0f}% (비싼 구간)"
        elif pctile >= 70:
            where = f"상위 {100 - pctile:.0f}%"
        elif pctile <= 10:
            where = f"하위 {pctile:.0f}% (싼 구간)"
        elif pctile <= 30:
            where = f"하위 {pctile:.0f}%"
        else:
            where = f"중간권({pctile:.0f}%)"

        return (f"{label} {p['현재']} — 자기역사 {p['역사연수']}년"
                f"({p['표본']}주) 중 {where}, 중앙값 {p['중앙값']}")


# ── 단독 실행: 적재 상태 점검 ─────────────────────────
def main():
    vh = ValuationHistory.load()
    if not vh.tickers:
        print("적재된 종목이 없습니다.")
        print("대화에서 price_multiple_data(scope='firm_history', format='json')로 받아")
        print("build_valuation_history.py 로 합치면 됩니다.")
        return 1

    days, fresh = vh.staleness()
    print(f"적재 종목 {len(vh.tickers)}개 · "
          f"{'최신' if fresh else '갱신 필요'}"
          f"{f' ({days}일 전 적재)' if days is not None else ''}")
    print(f"정의: {vh.정의}\n")

    enough = short = 0
    for t in vh.tickers:
        p = vh.position(t, DEFAULT_KEY)
        name = (vh._by_ticker[t].get("종목명") or t)
        if p and not p["표본부족"]:
            enough += 1
            print(f"  {name}({t}) {vh.describe(t)}")
        else:
            short += 1
            print(f"  {name}({t}) {vh.describe(t)}")

    print(f"\n백분위 산출 가능 {enough}개 · 표본 부족 {short}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
