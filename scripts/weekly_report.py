#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
주간 리포트 — 매일 쌓인 기록을 주 1회 읽을 수 있는 한 장으로.

왜 주간인가
  영업이익률·PER·부채비율·지분은 분기나 연 단위로 변하는데 알림만
  하루 주기였다. 어제와 오늘이 거의 같은 내용을 99종목씩 보내니
  읽을 이유가 없어진다. 그래서 전송은 끄고(DAILY_TELEGRAM=off)
  주 1회 이 리포트로 본다. 적재는 매일 그대로다.

구조 — 맨 위가 '달라진 것'이다
  매주 99종목을 처음부터 다시 읽을 수는 없다. 지난주 대비
  골든존 진입·이탈과 점수 급변만 먼저 보고, 상세는 그 아래 둔다.

입력 (모두 data/ 아래, 없으면 그 칸만 비운다)
  matching_results.json        오늘의 판정 스냅샷      [필수]
  matching_history.jsonl       매일 적재분 → 주간 변화
  valuation_history.json       PER 자기역사 백분위     [선택]

출력
  data/weekly_report.html      자체완결 HTML (외부 로드 없음)

실행
  python scripts/weekly_report.py                  # 레포 파일에서
  python scripts/weekly_report.py --payload p.json # 미리 만든 페이로드에서
  python scripts/weekly_report.py --dump-payload p.json   # 페이로드만 저장
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")
RESULTS = os.path.join(DATA_DIR, "matching_results.json")
HISTORY = os.path.join(DATA_DIR, "matching_history.jsonl")
OUT_HTML = os.path.join(DATA_DIR, "weekly_report.html")

COMPARE_DAYS = 7          # 며칠 전과 비교할지
BIG_MOVE = 8.0            # 종합점수가 이만큼 움직이면 '급변'
MAX_HIDDEN = 12           # 숨은 우량은 상위 몇 개까지 표에 실을지
FUNDA_GOOD = 60           # 매칭 엔진의 FUNDA_GOOD_THRESHOLD 와 같은 값


# ── 유틸 ────────────────────────────────────────────
def strip_emoji_label(label: str) -> str:
    """'🎯 골든존 (체력 + 내러티브)' → '골든존 (체력 + 내러티브)'."""
    s = (label or "").strip()
    return s.split(" ", 1)[1] if " " in s and not s[0].isalnum() else s


def short_label(label: str) -> str:
    """표 안에서 쓸 짧은 이름. 괄호 설명은 뗀다 —
    폰 너비에서 '숨은 우량 (아직 소외)에서'가 세 줄로 접혔다."""
    return strip_emoji_label(label).split(" (")[0].strip()


def won(v) -> str:
    """원 단위를 조/억으로. 표에 들어가므로 짧게."""
    if not isinstance(v, (int, float)) or v <= 0:
        return "—"
    if v >= 1_0000_0000_0000:
        return f"{v / 1_0000_0000_0000:,.1f}조"
    return f"{v / 1_0000_0000:,.0f}억"


def num(v, suffix="", dash="—"):
    if not isinstance(v, (int, float)):
        return dash
    return f"{v:,.1f}{suffix}"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                     # noqa: BLE001
        return default


def load_history(path):
    """날짜·티커 중복을 제거해 돌려준다.

    같은 날 두 번 돌면 같은 (날짜, 티커)가 두 벌 쌓인다. 최신 timestamp만 남긴다.
    날짜 필드가 없는 레거시 기록은 비교에 쓸 수 없으므로 버리고 개수만 센다.
    """
    best, legacy = {}, 0
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
                if not r.get("날짜"):
                    legacy += 1
                    continue
                key = (r["날짜"], r.get("티커"))
                old = best.get(key)
                if old is None or str(r.get("timestamp", "")) > str(old.get("timestamp", "")):
                    best[key] = r
    except FileNotFoundError:
        return {}, 0
    return best, legacy


# ── 페이로드 만들기 ──────────────────────────────────
def build_payload() -> dict:
    results = load_json(RESULTS, None)
    if not results:
        raise SystemExit(f"{RESULTS} 를 읽을 수 없습니다. 매칭 엔진을 먼저 실행하세요.")

    rows = results.get("결과") or []
    hist, legacy = load_history(HISTORY)

    # 스냅샷 날짜는 생성시각(UTC)을 KST로 돌린 날이다.
    try:
        made = datetime.fromisoformat(str(results.get("생성시각")).replace("Z", "+00:00"))
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        today = made.astimezone(KST).strftime("%Y-%m-%d")
    except Exception:                                     # noqa: BLE001
        today = datetime.now(KST).strftime("%Y-%m-%d")

    days = sorted({d for d, _ in hist})
    prev = None
    if days:
        target = (datetime.strptime(today, "%Y-%m-%d")
                  - timedelta(days=COMPARE_DAYS)).strftime("%Y-%m-%d")
        earlier = [d for d in days if d <= target]
        prev = earlier[-1] if earlier else (days[0] if days[0] < today else None)

    def snapshot(day):
        return {t: r for (d, t), r in hist.items() if d == day}

    cur_h = snapshot(today)
    prv_h = snapshot(prev) if prev else {}

    def is_golden(rec):
        return bool(rec) and "골든존" in str(rec.get("라벨", ""))

    enter, exited, moves = [], [], []
    for tk, a in cur_h.items():
        b = prv_h.get(tk)
        if is_golden(a) and not is_golden(b):
            enter.append({"종목명": a.get("종목명"), "티커": tk, "점수": a.get("종합점수"),
                          "변화": (round(a["종합점수"] - b["종합점수"], 1) if b else None),
                          "이전": short_label(b.get("라벨")) if b else "유니버스 신규"})
        if b and is_golden(b) and not is_golden(a):
            exited.append({"종목명": a.get("종목명"), "티커": tk, "점수": a.get("종합점수"),
                           "변화": round(a["종합점수"] - b["종합점수"], 1),
                           "현재": short_label(a.get("라벨"))})
        if b and isinstance(a.get("종합점수"), (int, float)) \
                and isinstance(b.get("종합점수"), (int, float)):
            diff = a["종합점수"] - b["종합점수"]
            if abs(diff) >= BIG_MOVE:
                moves.append({"종목명": a.get("종목명"), "티커": tk,
                              "이전": b["종합점수"], "현재": a["종합점수"],
                              "변화": round(diff, 1)})
    moves.sort(key=lambda m: -abs(m["변화"]))

    # PER 자기역사 (있으면)
    per_pos = {}
    try:
        sys.path.insert(0, HERE)
        from valuation_history import ValuationHistory      # noqa: PLC0415
        vh = ValuationHistory.load()
        for tk in {r.get("티커") for r in rows}:
            p = vh.position(tk, "per_fy0")
            if p and not p.get("표본부족"):
                per_pos[tk] = p
    except Exception:                                     # noqa: BLE001
        pass

    def detail(r):
        f = r.get("펀더멘탈") or {}
        nr = r.get("내러티브") or {}
        z = r.get("지분지표") or {}
        c = f.get("현재") or {}
        vp = per_pos.get(r.get("티커"))
        return {
            "종목명": r.get("종목명"), "티커": r.get("티커"),
            "종합": r.get("종합점수"), "펀더": r.get("펀더멘탈점수"),
            "내러": nr.get("내러티브점수"), "출처수": nr.get("출처수") or 0,
            "PER표시": (f.get("PER요약") or {}).get("표시") or "",
            "PER백분위": vp.get("백분위") if vp else None,
            "PER표본": vp.get("표본") if vp else None,
            "영업이익률": c.get("영업이익률"), "부채비율": c.get("부채비율"),
            "매출성장률": c.get("매출성장률"), "배당률": c.get("시가배당률"),
            "유통비율": z.get("실질유통비율"),
            "수급": (r.get("수급신호") or [None])[0] or "",
            "지분": (r.get("지분플래그") or [None])[0] or "",
            "시총": r.get("시가총액"),
            # 블로거가 실제로 뭐라고 했는지. 점수만 보면 왜 뽑혔는지 알 수 없다.
            "근거": [str(x) for x in (nr.get("근거") or [])][:2],
            "출처목록": [str(x) for x in (nr.get("출처목록") or [])],
            "언급된이름": [str(x) for x in (nr.get("언급된이름") or [])],
        }

    by_label = defaultdict(int)
    for r in rows:
        by_label[strip_emoji_label(r.get("라벨"))] += 1

    golden = [detail(r) for r in rows if "골든존" in str(r.get("라벨"))]
    golden.sort(key=lambda d: -(d["종합"] or 0))
    hidden = [detail(r) for r in rows if "숨은 우량" in str(r.get("라벨"))]
    hidden.sort(key=lambda d: -(d["종합"] or 0))

    axes = {}
    for axis in (results.get("배점") or {}):
        axes[axis] = sum(1 for r in rows
                         if ((r.get("펀더멘탈") or {}).get("축점수") or {}).get(axis) is not None)

    return {
        "스냅샷": today, "비교기준": prev, "생성시각": results.get("생성시각"),
        "배점": results.get("배점") or {}, "기준": results.get("기준") or {},
        "총종목": len(rows), "미매칭": len(results.get("미매칭") or []),
        "라벨분포": dict(by_label), "축별산출": axes,
        "PER표시가능": sum(1 for r in rows
                       if ((r.get("펀더멘탈") or {}).get("PER요약") or {}).get("현재") is not None),
        "PER백분위가능": len(per_pos),
        "금주종목수": len(cur_h), "전주종목수": len(prv_h),
        "신규유니버스": len([t for t in cur_h if t not in prv_h]) if prv_h else None,
        "이탈유니버스": len([t for t in prv_h if t not in cur_h]) if prv_h else None,
        "골든존진입": enter, "골든존이탈": exited, "점수급변": moves[:10],
        "골든존": golden, "숨은우량": hidden[:MAX_HIDDEN], "숨은우량전체": len(hidden),
        "적재일수": len(days), "레거시기록": legacy,
    }


# ── HTML ────────────────────────────────────────────
def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def render_html(p: dict) -> str:
    chg_rows = []
    for it in p.get("골든존진입") or []:
        d = it.get("변화")
        chg_rows.append(
            f'<tr><td><span class="pill pill-in">진입</span></td>'
            f'<td class="nm">{e(it["종목명"])}<span class="tk">{e(it["티커"])}</span></td>'
            f'<td class="n">{num(it["점수"])}</td>'
            f'<td class="n {"up" if (d or 0) > 0 else ""}">{("+" + str(d)) if isinstance(d, (int, float)) and d > 0 else (d if d is not None else "—")}</td>'
            f'<td class="sub">{e(short_label(it["이전"]))}에서</td></tr>')
    for it in p.get("골든존이탈") or []:
        d = it.get("변화")
        chg_rows.append(
            f'<tr><td><span class="pill pill-out">이탈</span></td>'
            f'<td class="nm">{e(it["종목명"])}<span class="tk">{e(it["티커"])}</span></td>'
            f'<td class="n">{num(it["점수"])}</td>'
            f'<td class="n down">{d}</td>'
            f'<td class="sub">{e(short_label(it["현재"]))}로</td></tr>')
    changes = ("".join(chg_rows) or
               '<tr><td colspan="5" class="empty">골든존 구성에 변화가 없었습니다.</td></tr>')

    move_rows = "".join(
        f'<tr><td class="nm">{e(m["종목명"])}<span class="tk">{e(m["티커"])}</span></td>'
        f'<td class="n dim">{num(m["이전"])}</td><td class="arrow">→</td>'
        f'<td class="n">{num(m["현재"])}</td>'
        f'<td class="n {"up" if m["변화"] > 0 else "down"}">'
        f'{"+" if m["변화"] > 0 else ""}{m["변화"]}</td></tr>'
        for m in (p.get("점수급변") or [])
    ) or '<tr><td colspan="5" class="empty">8점 이상 움직인 종목이 없습니다.</td></tr>'

    def stock_table(items, show_owner=True):
        if not items:
            return '<p class="empty">해당 종목이 없습니다.</p>'
        out = []
        for d in items:
            per = e(d["PER표시"]) or "—"
            if d.get("PER백분위") is not None:
                per += (f' <span class="pct">자기역사 {d["PER백분위"]}%'
                        f'<span class="dimx"> · {d["PER표본"]}주</span></span>')
            extra = []
            if d["수급"]:
                extra.append(f'<span class="tag tag-sd">{e(d["수급"])}</span>')
            if show_owner and d["지분"]:
                extra.append(f'<span class="tag">{e(d["지분"])}</span>')

            # 블로거가 한 말. 점수가 왜 올라갔는지는 결국 여기에 있다.
            said = ""
            quotes = d.get("근거") or []
            srcs = d.get("출처목록") or []
            names = [x for x in (d.get("언급된이름") or []) if x != d["종목명"]]
            if quotes or srcs:
                head = ""
                if srcs:
                    head = ('<div class="said-src">'
                            + " · ".join(f"@{e(s)}" for s in srcs[:5])
                            + (f' <span class="dimx">외 {len(srcs) - 5}</span>'
                               if len(srcs) > 5 else "")
                            + "</div>")
                body = "".join(f'<p>{e(q[:200])}{"…" if len(q) > 200 else ""}</p>'
                               for q in quotes)
                alias = (f'<div class="said-alias">언급된 이름: {e(" / ".join(names[:3]))}</div>'
                         if names else "")
                said = f'<div class="said">{head}{body}{alias}</div>'
            out.append(f"""<article class="card">
  <header>
    <h3>{e(d["종목명"])}<span class="tk">{e(d["티커"])}</span></h3>
    <div class="score"><b>{num(d["종합"])}</b><span>종합</span></div>
  </header>
  <div class="bars">
    <div class="bar"><span>펀더 {num(d["펀더"])}</span>
      <i style="width:{min(100, max(0, d["펀더"] or 0)):.0f}%"></i></div>
    <div class="bar nv"><span>내러 {num(d["내러"])} <em>출처 {d["출처수"]}</em></span>
      <i style="width:{min(100, max(0, d["내러"] or 0)):.0f}%"></i></div>
  </div>
  <dl class="facts">
    <div><dt>영업이익률</dt><dd>{num(d["영업이익률"], "%")}</dd></div>
    <div><dt>부채비율</dt><dd>{num(d["부채비율"], "%")}</dd></div>
    <div><dt>매출성장</dt><dd>{num(d["매출성장률"], "%")}</dd></div>
    <div><dt>배당</dt><dd>{num(d["배당률"], "%")}</dd></div>
    <div><dt>유통물량</dt><dd>{num(d["유통비율"], "%")}</dd></div>
    <div><dt>시총</dt><dd>{won(d["시총"])}</dd></div>
  </dl>
  <p class="per">{per}</p>
  {said}
  {'<p class="tags">' + "".join(extra) + '</p>' if extra else ''}
</article>""")
        return '<div class="cards">' + "".join(out) + "</div>"

    weights = " · ".join(f"{k} {v}" for k, v in (p.get("배점") or {}).items())
    axes = " · ".join(f"{k} {v}/{p['총종목']}" for k, v in (p.get("축별산출") or {}).items())
    labels = "".join(
        f'<div class="lab"><b>{v}</b><span>{e(k.split(" (")[0])}</span></div>'
        for k, v in sorted((p.get("라벨분포") or {}).items(), key=lambda x: -x[1]))

    per_line = (f'PER 자기역사 백분위 {p["PER백분위가능"]}/{p["총종목"]}종목'
                if p.get("PER백분위가능") else
                'PER 자기역사는 아직 적재 전입니다 — 네이버 3년치 표시만 나갑니다')

    uni = ""
    if p.get("신규유니버스") is not None:
        uni = (f'유니버스 {p["전주종목수"]} → {p["금주종목수"]}종목 '
               f'(신규 {p["신규유니버스"]} · 이탈 {p["이탈유니버스"]})')

    return f"""<title>골든존 주간 리포트</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>
/* 레이아웃: 단일 칼럼. 달라진 것이 맨 위, 상세는 카드로 아래. */
:root {{
  --bg:#f7f8fa; --panel:#ffffff; --ink:#16202e; --ink-2:#4a5a6e;
  --ink-3:#7d8a9c; --line:#dde3ea; --line-2:#eef2f6;
  --navy:#1b3a5c; --amber:#b06a12; --amber-bg:#fdf4e7;
  --up:#1a7f4b; --down:#b3261e; --up-bg:#e8f5ee; --down-bg:#fdeceb;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"IBM Plex Sans KR",system-ui,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#0e141c; --panel:#161e29; --ink:#e7edf5; --ink-2:#a9b7c8; --ink-3:#73839a;
  --line:#26313f; --line-2:#1d2733; --navy:#8fb6dd; --amber:#e0a258; --amber-bg:#2a2013;
  --up:#52c48a; --down:#f2857c; --up-bg:#13281d; --down-bg:#2b1715; color-scheme:dark;
}} }}
:root[data-theme="dark"] {{
  --bg:#0e141c; --panel:#161e29; --ink:#e7edf5; --ink-2:#a9b7c8; --ink-3:#73839a;
  --line:#26313f; --line-2:#1d2733; --navy:#8fb6dd; --amber:#e0a258; --amber-bg:#2a2013;
  --up:#52c48a; --down:#f2857c; --up-bg:#13281d; --down-bg:#2b1715; color-scheme:dark;
}}
body {{ background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:15px; line-height:1.6; margin:0; }}
.wrap {{ max-width:960px; margin:0 auto;
  padding-left:max(16px,env(safe-area-inset-left,0px));
  padding-right:max(16px,env(safe-area-inset-right,0px));
  padding-block:28px 56px; }}
h1,h2,h3 {{ text-wrap:balance; margin:0; font-weight:600; letter-spacing:-.01em; }}
h1 {{ font-size:1.6rem; }}
h2 {{ font-size:1.02rem; color:var(--navy); display:flex; align-items:baseline;
  gap:4px 10px; margin-bottom:12px; flex-wrap:wrap; }}
h2 small {{ font-weight:400; font-size:.78rem; color:var(--ink-3); letter-spacing:.02em; }}
.n, .score b, td.n {{ font-family:var(--mono); font-variant-numeric:tabular-nums; }}
header.top {{ display:flex; flex-wrap:wrap; gap:8px 18px; align-items:baseline;
  border-bottom:2px solid var(--navy); padding-bottom:12px; margin-bottom:26px; }}
header.top .meta {{ font-family:var(--mono); font-size:.8rem; color:var(--ink-3); }}
section {{ margin-bottom:34px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  overflow:hidden; }}
table {{ width:100%; border-collapse:collapse; font-size:.9rem; }}
th,td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--line-2);
  vertical-align:middle; }}
tr:last-child td {{ border-bottom:0; }}
td.n {{ text-align:right; white-space:nowrap; }}
td.arrow {{ color:var(--ink-3); padding:0; width:18px; text-align:center; }}
td.dim {{ color:var(--ink-3); }}
.nm {{ font-weight:500; min-width:0; }}
.tk {{ font-family:var(--mono); font-size:.72rem; color:var(--ink-3);
  margin-left:7px; font-weight:400; }}
.sub {{ color:var(--ink-3); font-size:.84rem; }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}
.pill {{ display:inline-block; font-size:.74rem; font-weight:600; padding:2px 9px;
  border-radius:999px; white-space:nowrap; }}
.pill-in {{ background:var(--up-bg); color:var(--up); }}
.pill-out {{ background:var(--down-bg); color:var(--down); }}
.empty {{ color:var(--ink-3); text-align:center; padding:18px; font-size:.88rem; }}
.scroll {{ overflow-x:auto; }}
.cards {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:14px 15px; min-width:0; }}
.card header {{ display:flex; justify-content:space-between; align-items:baseline;
  gap:10px; margin-bottom:10px; }}
.card h3 {{ font-size:1rem; min-width:0; }}
.score {{ display:flex; align-items:baseline; gap:5px; white-space:nowrap; }}
.score b {{ font-size:1.22rem; color:var(--navy); font-weight:600; }}
.score span {{ font-size:.7rem; color:var(--ink-3); }}
.bars {{ display:grid; gap:5px; margin-bottom:11px; }}
.bar {{ position:relative; background:var(--line-2); border-radius:4px; height:20px;
  overflow:hidden; }}
.bar i {{ position:absolute; inset:0 auto 0 0; background:var(--navy); opacity:.17; }}
.bar.nv i {{ background:var(--amber); opacity:.26; }}
.bar span {{ position:relative; font-size:.74rem; padding:0 8px; line-height:20px;
  display:block; color:var(--ink-2); }}
.bar em {{ font-style:normal; color:var(--ink-3); font-size:.69rem; }}
.facts {{ display:grid; grid-template-columns:repeat(3,1fr); gap:7px 4px; margin:0 0 10px; }}
.facts div {{ min-width:0; }}
.facts dt {{ font-size:.68rem; color:var(--ink-3); letter-spacing:.02em; }}
.facts dd {{ margin:0; font-family:var(--mono); font-size:.84rem;
  font-variant-numeric:tabular-nums; }}
.per {{ margin:0; font-size:.78rem; color:var(--ink-2); background:var(--amber-bg);
  border-radius:6px; padding:6px 9px; }}
.pct {{ color:var(--amber); font-weight:600; }}
.dimx {{ color:var(--ink-3); font-weight:400; }}
.said {{ margin-top:10px; border-left:2px solid var(--line); padding-left:10px; }}
.said-src {{ font-family:var(--mono); font-size:.7rem; color:var(--navy);
  margin-bottom:5px; word-break:normal; overflow-wrap:anywhere; }}
.said p {{ margin:0 0 5px; font-size:.79rem; color:var(--ink-2); line-height:1.55; }}
.said p:last-of-type {{ margin-bottom:0; }}
.said-alias {{ font-size:.71rem; color:var(--ink-3); margin-top:5px; }}
.tags {{ margin:9px 0 0; display:flex; flex-wrap:wrap; gap:5px; }}
.tag {{ font-size:.71rem; color:var(--ink-2); background:var(--line-2);
  border-radius:5px; padding:3px 7px; }}
.tag-sd {{ color:var(--navy); }}
.labs {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
.lab {{ background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:9px 13px; min-width:92px; }}
.lab b {{ display:block; font-family:var(--mono); font-size:1.18rem; color:var(--navy); }}
.lab span {{ font-size:.73rem; color:var(--ink-3); }}
.note {{ font-size:.82rem; color:var(--ink-2); background:var(--panel);
  border:1px solid var(--line); border-left:3px solid var(--amber);
  border-radius:8px; padding:11px 14px; margin:0 0 10px; }}
.note b {{ color:var(--amber); }}
.foot {{ font-size:.78rem; color:var(--ink-3); border-top:1px solid var(--line);
  padding-top:14px; line-height:1.75; }}
.readme {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:20px 22px; }}
.readme h3 {{ font-size:.9rem; margin:20px 0 7px; color:var(--ink); }}
.readme h3:first-of-type {{ margin-top:4px; }}
.readme p {{ margin:0 0 9px; font-size:.86rem; color:var(--ink-2); max-width:68ch; }}
.readme b {{ color:var(--ink); font-weight:600; }}
.readme code {{ font-family:var(--mono); font-size:.8rem; background:var(--line-2);
  padding:1px 5px; border-radius:4px; }}
.defs {{ margin:0 0 9px; display:grid; gap:7px; }}
.defs div {{ display:grid; grid-template-columns:106px 1fr; gap:10px; align-items:baseline; }}
.defs dt {{ font-size:.82rem; font-weight:600; color:var(--navy); }}
.defs dd {{ margin:0; font-size:.84rem; color:var(--ink-2); min-width:0; }}
@media (max-width:480px) {{ .defs div {{ grid-template-columns:1fr; gap:1px; }} }}
@media (max-width:560px) {{
  /* 변화 표의 '어디에서/어디로'는 폰에서 세 줄로 접혀 표를 망가뜨린다.
     진입·이탈 pill 과 점수만 남기면 뜻은 그대로다. */
  td.sub {{ display:none; }}
  th,td {{ padding:9px 8px; }}
  .nm {{ word-break:keep-all; }}
}}
@media (max-width:420px) {{
  .facts {{ grid-template-columns:repeat(2,1fr); }}
  h1 {{ font-size:1.35rem; }}
  .cards {{ grid-template-columns:1fr; }}
}}
</style>
<div class="wrap">
  <header class="top">
    <h1>골든존 주간 리포트</h1>
    <span class="meta">{e(p["스냅샷"])} 기준
      {f'· 지난주 {e(p["비교기준"])} 대비' if p.get("비교기준") else '· 비교할 지난주 기록 없음'}</span>
  </header>

  <section>
    <h2>이번 주 달라진 것 <small>{e(uni)}</small></h2>
    <div class="panel scroll">
      <table><tbody>{changes}</tbody></table>
    </div>
  </section>

  <section>
    <h2>종합점수 급변 <small>{BIG_MOVE:.0f}점 이상 · 지난주 대비</small></h2>
    <div class="panel scroll">
      <table><tbody>{move_rows}</tbody></table>
    </div>
  </section>

  <section>
    <h2>골든존 <small>체력과 내러티브가 함께 선 종목</small></h2>
    {stock_table(p.get("골든존") or [])}
  </section>

  <section>
    <h2>숨은 우량 <small>체력은 있는데 아직 소외 · 상위 {len(p.get("숨은우량") or [])}/{p.get("숨은우량전체", 0)}개</small></h2>
    {stock_table(p.get("숨은우량") or [])}
  </section>

  <section>
    <h2>엔진 상태</h2>
    <div class="labs">{labels}</div>
    <p class="note"><b>읽는 법.</b> 내러티브 점수는 출처 수에 크게 좌우됩니다 —
      블로그 한 곳이 두 곳이 되면 18점이 올라갑니다. 골든존 진입·이탈과
      점수 급변의 상당 부분이 실적이 아니라 언급 수 변화입니다.
      펀더멘탈 점수가 함께 움직였는지 같이 보셔야 합니다.</p>
    <p class="foot">
      적재 {e(p.get("적재일수"))}일치 · 분석 {e(p["총종목"])}종목 · 미매칭 {e(p["미매칭"])}개{f" · 날짜 없는 레거시 기록 {p['레거시기록']}건" if p.get("레거시기록") else ""}<br>
      펀더멘탈 배점 {e(weights)}<br>
      축별 산출 {e(axes)}<br>
      PER 표시 가능 {e(p["PER표시가능"])}/{e(p["총종목"])}종목 · {e(per_line)}<br>
      수급은 네이버 제공 <b>KRX 단일시장</b> 기준입니다. HTS를 KRX+NXT 통합으로 보시면
      수치가 다르고, 순매수 규모가 작은 날은 부호까지 뒤바뀔 수 있습니다.<br>
      부채비율 절대기준은 금융업(증권·은행·보험)에 맞지 않습니다.
      PER은 점수에 넣지 않고 숫자만 보여줍니다.
    </p>
  </section>

  <section class="readme">
    <h2>이 페이지는 무엇인가</h2>

    <h3>어떻게 만들어지나</h3>
    <p>매일 새벽 파이프라인이 두 축을 따로 잽니다. <b>펀더멘탈</b>은 DART 연간 재무에서
      최대 12년치를 끌어와, 그 회사가 자기 과거 대비 지금 어디쯤인지를 백분위로 냅니다.
      <b>내러티브</b>는 블로그·유튜브에서 최근 {e((p.get("기준") or {}).get("언급조회기간일", 14))}일간
      그 종목이 몇 곳에서 언급됐는지를 셉니다. 두 축이 함께 서면 골든존입니다.</p>

    <h3>라벨</h3>
    <dl class="defs">
      <div><dt>골든존</dt><dd>펀더멘탈 {FUNDA_GOOD}점 이상 + 내러티브
        {e((p.get("기준") or {}).get("내러티브하한", 50))}점 이상·출처 2곳 이상. 체력과 관심이 함께 선 상태입니다.</dd></div>
      <div><dt>숨은 우량</dt><dd>체력은 기준을 넘었는데 아직 언급이 적은 종목. 선행 후보입니다.</dd></div>
      <div><dt>내러티브 선행</dt><dd>언급은 많은데 체력이 못 따라오는 종목. 이야기가 먼저 간 경우입니다.</dd></div>
      <div><dt>관망 · 적자</dt><dd>두 축 모두 기준 미달이거나, 적자라 펀더멘탈 판정에서 뺀 종목입니다.</dd></div>
    </dl>

    <h3>점수 읽는 법</h3>
    <p><b>펀더멘탈</b>은 {e(weights)} 배점의 가중평균입니다. 확보된 지표의 배점만으로
      환산하되, 모르는 축이 많으면 점수를 위로 올려주지 않습니다 —
      모르면 좋게 보지 않되, 나쁘다는 판정은 그대로 둡니다.</p>
    <p><b>내러티브</b>는 출처 수가 거의 다 결정합니다. 공식이
      <code>10 + (출처수−1)×18</code>이라, 블로그 한 곳이 두 곳이 되면 18점이 뜁니다.
      이번 주 점수 급변의 상당 부분이 실적이 아니라 언급 수 변화인 이유입니다.</p>
    <p><b>PER</b>은 점수에 넣지 않습니다. 업종이 같아도 기술과 주력 제품이 달라
      동종업계 비교가 신뢰하기 어려웠기 때문입니다. 대신 그 종목의 <b>자기 과거</b>
      주간 시계열과 비교해 백분위만 보여줍니다. 백분위 90%면 자기 역사에서
      상위 10% 구간, 즉 비싼 쪽입니다. 표본이 104주(2년) 미만이면 판정을 보류합니다.</p>

    <h3>이 페이지가 답하지 못하는 것</h3>
    <p>언제 사고 팔지는 답하지 않습니다. 이 페이지는 <b>지금 어디쯤인가</b>를 보는 도구이고,
      매수·매도 판단은 여기 없습니다. 내러티브는 블로그 언급 수일 뿐 그 내용의
      사실 여부를 검증하지 않습니다. 수급은 KRX 단일시장 기준이라 통합 수치와 다릅니다.</p>

    <h3>알림은 왜 안 오나</h3>
    <p>일일 텔레그램 알림은 꺼져 있습니다. 영업이익률·PER·부채비율·지분은 분기나 연 단위로
      변하는데 알림만 하루 주기여서, 어제와 오늘이 거의 같은 내용이 반복됐기 때문입니다.
      적재는 매일 그대로 돌고 있고, 파이프라인 고장 경고는 계속 옵니다.
      <b>다시 켜는 조건은 미리 정해뒀습니다</b> — 엔진 자기평가가 골든존의 코스피 대비
      초과수익을 종목 30개 이상에서 보여줄 때입니다. 나중에 감으로 판단하지 않기 위해서입니다.</p>
  </section>
</div>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", help="미리 만든 페이로드 JSON에서 렌더")
    ap.add_argument("--dump-payload", help="페이로드만 이 경로에 저장")
    ap.add_argument("--out", default=OUT_HTML)
    args = ap.parse_args()

    if args.payload:
        with open(args.payload, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = build_payload()

    if args.dump_payload:
        with open(args.dump_payload, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print(f"페이로드 저장: {args.dump_payload}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render_html(payload))

    size = os.path.getsize(args.out) / 1024
    print(f"주간 리포트 생성: {args.out} ({size:,.0f} KB)")
    print(f"  스냅샷 {payload['스냅샷']} · 비교 {payload.get('비교기준') or '없음'}")
    print(f"  골든존 {len(payload.get('골든존') or [])}개 "
          f"· 진입 {len(payload.get('골든존진입') or [])} "
          f"· 이탈 {len(payload.get('골든존이탈') or [])} "
          f"· 급변 {len(payload.get('점수급변') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
