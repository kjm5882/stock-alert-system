#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
밸류에이션 자기역사 적재기 — MCP 응답 덩어리를 data/valuation_history.json 으로.

쓰는 순서
  1. 대화에서 종목별로 호출:
       price_multiple_data(company="종목명", scope="firm_history", format="json")
  2. 각 응답을 한 디렉터리에 .json 또는 .txt 로 떨어뜨린다
     (도구 결과가 파일로 저장되면 그 경로를 그대로 쓰면 된다)
  3. python scripts/build_valuation_history.py <그 디렉터리> [<디렉터리2> ...]

왜 합치는 스크립트가 따로 있나
  적재를 재현 가능하게 만들기 위해서다. 손으로 붙이면 다음에 갱신할 때
  무엇을 어떻게 넣었는지 알 수 없다. 이 스크립트는
  **기존 파일을 읽어 종목별로 병합**하므로, 몇 종목만 다시 받아
  덮어써도 나머지가 날아가지 않는다.

입력 형태 두 가지를 모두 받는다
  - {"result": "<JSON 문자열>"}   ← 도구 결과 래퍼
  - {"tool": ..., "data": {...}}  ← 벗겨진 본문
"""

from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "..", "data", "valuation_history.json")

COLUMNS = ["asof", "per_fy0", "per_ttm", "pbr"]   # 적재할 열. 순서 고정.

DEFINITION = ("PER = 보통주 시총 ÷ 지배주주순이익 (PIT, 주간) · "
              "PBR = 보통주 시총 ÷ 지배자본. "
              "출처 open-proxy-mcp price_multiple_data(scope=firm_history). "
              "네이버 PER(주가÷EPS)과 정의가 달라 섞지 말 것.")


def unwrap(raw: str) -> dict | None:
    """래퍼를 벗겨 본문 dict 를 돌려준다."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        try:
            obj = json.loads(obj["result"])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def extract(obj: dict) -> tuple[str, dict] | None:
    """본문에서 (티커, 적재레코드) 를 뽑는다. 못 뽑으면 None."""
    data = obj.get("data") or {}
    ticker = str(data.get("ticker") or "").strip()
    series = data.get("series")
    if not ticker or not isinstance(series, list) or not series:
        return None

    rows = []
    for r in series:
        if not isinstance(r, dict) or not r.get("asof"):
            continue
        row = [str(r["asof"])]
        for col in COLUMNS[1:]:
            v = r.get(col)
            row.append(round(float(v), 4) if isinstance(v, (int, float)) else None)
        rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])

    # subject 는 문자열("한양이엔지")로 오기도 하고 dict 로 오기도 한다.
    # 타입을 가정하지 말고 둘 다 받는다.
    subject = obj.get("subject")
    if isinstance(subject, str):
        name = subject.strip()
    elif isinstance(subject, dict):
        name = str(subject.get("name") or subject.get("company") or "").strip()
    else:
        name = ""
    if not name:
        name = str(data.get("name") or "").strip()

    return ticker, {
        "종목명": name,
        "시장": data.get("market") or "",
        "섹터": str(data.get("sector") or ""),
        "갱신일": rows[-1][0],
        "관측수": len(rows),
        "열": COLUMNS,
        "series": rows,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    paths: list[str] = []
    for arg in argv[1:]:
        if os.path.isdir(arg):
            for pat in ("*.json", "*.txt"):
                paths.extend(sorted(glob.glob(os.path.join(arg, pat))))
        else:
            paths.append(arg)
    if not paths:
        print("입력 파일이 없습니다.")
        return 1

    # 기존 적재분을 먼저 읽는다 — 부분 갱신이 되게.
    merged: dict = {}
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                merged = json.load(f)
            before = len([k for k in merged if not k.startswith("_")])
            print(f"기존 적재분 {before}종목을 읽었습니다 — 병합합니다.")
        except Exception as exc:                          # noqa: BLE001
            print(f"기존 파일 읽기 실패({str(exc)[:60]}) — 새로 만듭니다.")
            merged = {}

    added = updated = skipped = 0
    for p in paths:
        try:
            raw = open(p, encoding="utf-8").read()
        except Exception:                                 # noqa: BLE001
            skipped += 1
            continue
        obj = unwrap(raw)
        if obj is None:
            skipped += 1
            continue
        got = extract(obj)
        if got is None:
            print(f"  [건너뜀] {os.path.basename(p)} — series 없음"
                  f"{' (status=' + str(obj.get('status')) + ')' if obj.get('status') else ''}")
            skipped += 1
            continue
        ticker, rec = got
        if ticker in merged:
            updated += 1
        else:
            added += 1
        merged[ticker] = rec
        print(f"  {rec['종목명'] or ticker}({ticker}) "
              f"{rec['관측수']}주 · {rec['series'][0][0]}~{rec['갱신일']}")

    merged["_생성"] = datetime.now(KST).isoformat(timespec="seconds")
    merged["_정의"] = DEFINITION
    merged["_열"] = COLUMNS

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(OUT_PATH) / 1024
    total = len([k for k in merged if not k.startswith("_")])
    print(f"\n적재 완료 — 신규 {added} · 갱신 {updated} · 건너뜀 {skipped}")
    print(f"총 {total}종목 · {OUT_PATH} ({size:,.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
