#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ValHist PER 상한 제거 검증.

두산(000150)의 per_ttm 값은 저장소 valuation_history.json 에서 그대로 꺼낸
실제 값이다(비-null 153개, 소수 2자리). 지어낸 숫자가 아니다.

기대값은 브라우저에서 저장 파일 전체(93종목)에 같은 규칙을 돌려 얻은 독립
측정치다 — 옛 규칙 114표본 81.6% / 새 규칙 153표본 60.8%.
Python 구현이 그 둘을 모두 재현해야 한다. 옛 값까지 재현하는 이유는,
'고쳤다'는 말이 성립하려면 고치기 전 상태도 같아야 하기 때문이다.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import red_team as rt                                                   # noqa: E402

# 두산 per_ttm — 실제 값, 시간순, null 제외 (null 은 백분위 계산에 안 쓰인다)
DOOSAN = [
    1.54, 1.76, 1.66, 1.6, 1.76, 1.75, 2.99, 3.05, 3.14, 3.72, 3.56, 3.38,
    3.23, 3.2, 3.03, 3.21, 3.58, 3.97, 3.71, 21.7, 20.45, 20.52, 21.39, 24.73,
    23.07, 20.99, 22.15, 21.84, 21.39, 22.76, 22.03, 22.1, 22.95, 12.63, 14.07,
    13.81, 14.51, 13.74, 14.28, 13.5, 13.87, 15.21, 15.08, 13.25, 13.56, 13.43,
    12.99, 12.02, 12.31, 12.91, 12.38, 12.8,
    22.08, 23.01, 24.12, 23.4, 25.77, 25, 23.96, 22.88, 25.56, 24.84, 26.54,
    34.4, 35.69, 54.89, 49.73, 44.78, 48.91, 49.73, 48.49, 48.7, 46.84, 46.43,
    42.92, 39.87, 41.48, 37.52, 39.83, 43.34, 52, 52.21, 48.7, 44.37, 8, 7.69,
    7.53, 7.45, 7, 6.86, 6.4, 70.45, 71.07, 76.2, 72.84, 61.96, 56.22, 61.61,
    60.2, 56.13, 58.69, 61.79, 63.11, 73.45,
    249.92, 235.85, 236.82, 250.88, 235.74, 205.46, 212.76, 206.27, 201.95,
    223.84, 255.74, 230.06, 223.84, 254.66, 283.05, 345.5, 283.86, 297.11,
    320.63, 325.22, 228.85, 273.73, 284.2, 329.28, 341.04, 370.74, 286.4,
    282.67, 349.93, 308.76, 336.44, 289.42, 255.88, 254.28, 245.23, 213.29,
    211.34, 207.61, 211.34, 56.18, 47.46, 54.51, 50.28, 59.7, 63.71, 66.57,
    59.04, 59.08, 53.76,
]


def vh_of(per_values, tk="000150", name="두산"):
    """valuation_history.json 의 실제 저장 형태로 감싼다."""
    return {tk: {
        "종목명": name, "관측수": len(per_values),
        "열": ["asof", "per_fy0", "per_ttm", "pbr", "cap_krw"],
        "series": [[f"2020{i:04d}", None, v, None, None]
                   for i, v in enumerate(per_values)],
    }}


def old_rule(per_values):
    """고치기 전 규칙 — 0.1~200 밖을 버린다. 비교 기준으로만 둔다."""
    vals = [v for v in per_values if 0.1 <= v <= 200.0]
    cur = vals[-1]
    return len(vals), round(sum(1 for v in vals if v <= cur) / len(vals) * 100, 1)


def main():
    fails = []

    print("1. 두산 실제 계열 — 옛 규칙과 새 규칙을 모두 재현하는가")
    on, op = old_rule(DOOSAN)
    print(f"   옛 규칙 재현: 표본 {on} · 백분위 {op}%  (측정치 114 / 81.6%)")
    if (on, op) != (114, 81.6):
        fails.append(f"옛 규칙 재현 실패: {on}/{op} != 114/81.6")

    pos = rt.ValHist(vh_of(DOOSAN)).ttm_position("000150")
    print(f"   새 규칙 실측: 표본 {pos['표본']} · 백분위 {pos['백분위']}% "
          f"· 현재 {pos['현재']} · 중앙값 {pos['중앙값']} "
          f"· 충분 {pos['충분']} · 무의미 {pos['무의미']}")
    print(f"   기대       : 표본 153 · 백분위 60.8% · 현재 53.8 · 무의미 False")
    if pos["표본"] != 153:
        fails.append(f"표본 {pos['표본']} != 153")
    if abs(pos["백분위"] - 60.8) > 0.1:
        fails.append(f"백분위 {pos['백분위']} != 60.8")
    if pos["무의미"]:
        fails.append("두산은 현재 53.76 이라 무의미가 아니어야 한다")
    if not pos["충분"]:
        fails.append("153표본은 충분해야 한다")

    print(f"\n   → 백분위 {op}% → {pos['백분위']}% ({pos['백분위'] - op:+.1f}%p)")
    f = []
    rt.check_valuation("000150", {}, rt.ValHist(vh_of(DOOSAN)), f)
    print(f"   → 새 규칙에서 지적 {len(f)}건 "
          f"{[x['주장'] for x in f] or '(없음 — 거짓 경고가 사라졌다)'}")
    if f:
        fails.append(f"두산은 백분위 60.8 이라 지적이 없어야 한다: {f}")
    # 옛 규칙이라면 81.6 >= 80 이라 '비싼 구간' 이 떴을 것이다 — 그게 거짓 경고였다
    if op < rt.PER_PCTILE_HIGH:
        fails.append("옛 규칙에서 80 미만이면 거짓 경고 서사가 성립하지 않는다")

    print("\n2. 분기 — 무의미 / 비쌈 / 침묵")
    # 현재값만 바꿔 분기를 본다. 앞 152개는 두산 실제 값으로 분포를 채운다.
    base = DOOSAN[:-1]
    # 🔴 두산 분포에서는 현재 200 이어도 백분위가 74.5% 다 (200~370 구간이
    #    상위 25% 를 채우고 있다). 그래서 '비쌈' 분기는 두산으로 못 만든다 —
    #    1~110 균등 분포에 현재 100(백분위 90.9%)을 쓴다.
    rising = [float(i) for i in range(1, 110)] + [100.0]
    cases = [
        ("무의미(현재 201)",       base + [201.0],   "뜻을 잃는",  True),
        ("무의미(현재 4929)",      base + [4928.63], "뜻을 잃는",  True),
        ("비쌈(현재 100/백분위91)", rising,           "비싼 구간",  True),
        ("침묵(현재 200/백분위75)", base + [200.0],   None,        False),
        ("침묵(현재 10/백분위18)",  base + [10.0],    None,        False),
    ]
    for label, series, want, should_fire in cases:
        p = rt.ValHist(vh_of(series)).ttm_position("000150")
        g = []
        rt.check_valuation("000150", {}, rt.ValHist(vh_of(series)), g)
        got = g[0]["주장"] if g else None
        ok = (bool(g) == should_fire) and (want is None or (got and want in got))
        print(f"   {'OK ' if ok else 'FAIL'} {label:<24s} 백분위 {p['백분위']:5.1f}% "
              f"무의미 {str(p['무의미']):<5s} → {got or '침묵'}")
        if not ok:
            fails.append(f"분기 {label}: 기대 {want}/{should_fire}, 실제 {got}")

    print("\n3. 표본 부족이면 무의미보다 보류가 먼저인가 (삼천당제약형)")
    short = [300.0 + i for i in range(50)] + [277.68]        # 51표본 < 104
    p = rt.ValHist(vh_of(short)).ttm_position("000150")
    g = []
    rt.check_valuation("000150", {"PER이력연수": 3}, rt.ValHist(vh_of(short)), g)
    got = g[0]["주장"] if g else None
    ok = p["무의미"] and not p["충분"] and got and "보류" in got
    print(f"   {'OK ' if ok else 'FAIL'} 표본 {p['표본']} 충분 {p['충분']} "
          f"무의미 {p['무의미']} → {got}")
    if not ok:
        fails.append(f"표본부족 우선순위: {got}")

    print("\n4. 경계·결측")
    edge = [
        ("하한 미만만",      [0.05, 0.09]),
        ("빈 계열",          []),
        ("전부 None",        None),
    ]
    for label, series in edge:
        try:
            if series is None:
                raw = {"000150": {"열": ["asof", "per_fy0", "per_ttm"],
                                  "series": [["x", None, None], ["y", None, None]]}}
            else:
                raw = vh_of(series)
            r = rt.ValHist(raw).ttm_position("000150")
            ok = r is None
            print(f"   {'OK ' if ok else 'FAIL'} {label:<14s} → {r}")
            if not ok:
                fails.append(f"{label}: None 이어야 하는데 {r}")
        except Exception as exc:                                  # noqa: BLE001
            print(f"   FAIL {label:<14s} {type(exc).__name__}: {exc}")
            fails.append(f"{label}: {exc}")

    print("\n" + "=" * 62)
    if fails:
        print(f"실패 {len(fails)}건")
        for x in fails:
            print(f"  - {x}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
