#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
엔진 자기평가 — 매칭 엔진의 판정이 실제로 맞았는지 채점한다.

무엇을 하는가
    matching_history.jsonl 에 쌓인 '그날의 판정'을 읽어서,
    판정일 이후 1·3·6·12개월 수익률과 코스피 대비 초과수익률을 계산하고
    라벨별·점수구간별로 집계한다.

왜 전 종목을 보는가
    골든존만 모아 평균을 내면 그 숫자가 좋아도 의미가 없다.
    같은 기간 관망 종목이 더 올랐을 수도 있기 때문이다.
    '골든존이 관망보다 나았나'가 판별력이고, 그러려면 관망도 채점해야 한다.

■ 어떻게 세는가 — 두 단계 평균 (이 파일에서 가장 중요한 부분)

    판정 하나하나를 전부 채점하되, 평균을 두 번에 나눠 낸다.
        1단계: 같은 라벨 안에서 '종목별'로 평균  → 종목의 대표값
        2단계: 그 대표값들을 다시 평균          → 라벨의 최종 숫자

    이렇게 하는 이유가 두 가지다.

    (1) 오래 머문 종목이 표본을 독차지하는 것을 막는다.
        30일 연속 골든존인 삼성전자는 판정 30건이지만 종목 대표값은 하나다.

    (2) 경계선에서 하루걸러 라벨이 뒤집히는 종목을 막는다.
        '라벨이 바뀐 날을 신호로 센다'는 방식은 여기서 무너진다.
        하루걸러 뒤집히면 20거래일에 신호가 20건이 되어, 중복 제거가 아니라
        신호 폭증이 된다. 실제로 펀더멘탈 60점 기준선 근처에는
        삼성전기(59.6) 한화엔진(58.3)처럼 조금만 움직여도 넘나드는 종목이 있다.
        두 단계 평균은 그 종목이 골든존 쪽에도 관망 쪽에도 '종목 하나'로만
        들어가게 만든다. 쿨다운 일수 같은 임의의 상수가 필요 없다.

    그래서 표본의 단위는 '신호 건수'가 아니라 '종목 수'다.
    표에 판정일수와 종목수를 함께 적는 이유가 그것이다.

■ 읽는 법
    종목수가 적으면 평균은 우연이다. 종목 10개 미만은 ⚠︎ 로 표시하고,
    그 숫자로는 가중치를 바꾸지 않는다.

실행:  python scripts/engine_eval.py
"""

from __future__ import annotations

import bisect
import json
import os
import re
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
HISTORY_FILE = os.path.join(DATA_DIR, "matching_history.jsonl")
EVAL_FILE = os.path.join(DATA_DIR, "engine_eval.json")
REPORT_FILE = os.path.join(DATA_DIR, "engine_eval.md")

PAUSE_SEC = 0.25
LEAD_DAYS = 10                # 판정일이 휴장일일 때를 대비해 앞으로 더 받는다
MIN_TICKERS = 10              # 이보다 종목이 적으면 평균을 신뢰하지 않는다는 표시
UNSTABLE_CHANGES = 4          # 30일 안에 라벨이 이만큼 바뀌면 '판정 불안정'으로 본다

INTERVALS = [("1개월", 30), ("3개월", 91), ("6개월", 182), ("12개월", 365)]

SCORE_BANDS = [("70점+", 70, 999), ("60~70", 60, 70),
               ("50~60", 50, 60), ("50점 미만", -999, 50)]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}


# ── 시세 조회 ────────────────────────────────────────────


def fetch_daily_closes(kind, code, start, end):
    """일별 종가 [(date, close), ...]. kind 는 'item' 또는 'index'."""
    url = f"https://api.stock.naver.com/chart/domestic/{kind}/{code}/day"
    params = {"startDateTime": start.strftime("%Y%m%d") + "0000",
              "endDateTime": end.strftime("%Y%m%d") + "2359"}
    res = requests.get(url, params=params, headers=NAVER_HEADERS, timeout=20)
    res.raise_for_status()
    rows = res.json()
    if isinstance(rows, dict):
        for key in ("priceInfos", "result", "data", "list"):
            if key in rows:
                rows = rows[key]
                break
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = _pick(row, ("localDate", "localDateTime", "date", "dt"))
        raw_close = _pick(row, ("closePrice", "close_price", "close", "clsprc"))
        if raw_date is None or raw_close is None:
            continue
        digits = re.sub(r"\D", "", str(raw_date))[:8]
        if len(digits) != 8:
            continue
        try:
            out.append((datetime.strptime(digits, "%Y%m%d").date(),
                        float(str(raw_close).replace(",", ""))))
        except ValueError:
            continue
    out.sort(key=lambda p: p[0])
    return out


def _pick(row, keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


class PriceSeries:
    """날짜로 종가를 빠르게 찾기 위한 얇은 래퍼.

    판정이 수만 건까지 늘어나므로 매번 앞에서부터 훑으면 느려진다.
    """

    def __init__(self, closes):
        self.days = [d for d, _ in closes]
        self.values = [v for _, v in closes]

    def __bool__(self):
        return bool(self.days)

    def on_or_after(self, target):
        idx = bisect.bisect_left(self.days, target)
        return self.values[idx] if idx < len(self.days) else None


def pct(before, after):
    if not before or not after:
        return None
    return round((after / before - 1) * 100, 2)


# ── 이력 읽기 ────────────────────────────────────────────


def load_records():
    """이력 파일 → 판정 목록 전체. 중복 제거는 하지 않는다(집계 단계에서 처리)."""
    if not os.path.exists(HISTORY_FILE):
        print(f"이력 파일이 없습니다: {HISTORY_FILE}")
        return [], 0

    rows, legacy = [], 0
    for line in open(HISTORY_FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 옛 형식(골든존 종목명만 담던 줄)은 티커가 없어 채점할 수 없다.
        if not row.get("티커") or not row.get("날짜"):
            legacy += 1
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r["날짜"], r["티커"]))
    return rows, legacy


# ── 채점 ────────────────────────────────────────────────


def grade(records, today):
    """각 판정에 구간별 수익률·초과수익률을 붙인다."""
    by_ticker = defaultdict(list)
    for row in records:
        by_ticker[row["티커"]].append(row)

    earliest = min(datetime.strptime(r["날짜"], "%Y-%m-%d").date() for r in records)
    kospi = PriceSeries(fetch_daily_closes(
        "index", "KOSPI", earliest - timedelta(days=LEAD_DAYS), today))
    if not kospi:
        print("코스피 지수를 받지 못했습니다. 초과수익률을 계산할 수 없습니다.")

    graded, failed = [], []
    for ticker, rows in sorted(by_ticker.items()):
        start = min(datetime.strptime(r["날짜"], "%Y-%m-%d").date() for r in rows)
        try:
            closes = PriceSeries(fetch_daily_closes(
                "item", ticker, start - timedelta(days=LEAD_DAYS), today))
            time.sleep(PAUSE_SEC)
        except Exception as exc:                      # noqa: BLE001
            failed.append((ticker, rows[0]["종목명"], str(exc)[:60]))
            continue
        if not closes:
            # 상장폐지·거래정지면 여기로 온다. 집계에서 빠지므로 건수를 보고한다.
            failed.append((ticker, rows[0]["종목명"], "종가 없음(상장폐지·거래정지 가능)"))
            continue

        for row in rows:
            day = datetime.strptime(row["날짜"], "%Y-%m-%d").date()
            base = closes.on_or_after(day) or row.get("판정일종가")
            kbase = kospi.on_or_after(day)
            if not base:
                continue
            item = dict(row)
            item["기준가"] = base
            for tag, days in INTERVALS:
                target = day + timedelta(days=days)
                if target > today:
                    item[f"초과_{tag}"] = None
                    continue
                s_pct = pct(base, closes.on_or_after(target))
                k_pct = pct(kbase, kospi.on_or_after(target))
                item[f"초과_{tag}"] = (round(s_pct - k_pct, 2)
                                     if s_pct is not None and k_pct is not None else None)
            graded.append(item)
    return graded, failed


# ── 집계 (두 단계 평균) ──────────────────────────────────


def summarize(graded, key_fn, label):
    """key_fn 으로 묶되, 종목별 평균을 먼저 내고 그것들을 다시 평균한다."""
    buckets = defaultdict(lambda: defaultdict(list))   # {구분: {티커: [판정, ...]}}
    for row in graded:
        key = key_fn(row)
        if key is not None:
            buckets[key][row["티커"]].append(row)

    table = []
    for key, per_ticker in buckets.items():
        entry = {
            "구분": key,
            "종목수": len(per_ticker),
            "판정일수": sum(len(v) for v in per_ticker.values()),
        }
        for tag, _ in INTERVALS:
            # 1단계 — 종목 안에서 평균
            per_ticker_means = []
            for rows in per_ticker.values():
                vals = [r[f"초과_{tag}"] for r in rows if r.get(f"초과_{tag}") is not None]
                if vals:
                    per_ticker_means.append(statistics.mean(vals))
            # 2단계 — 종목들 사이에서 평균
            entry[tag] = {
                "종목수": len(per_ticker_means),
                "평균": round(statistics.mean(per_ticker_means), 2) if per_ticker_means else None,
                "중앙값": (round(statistics.median(per_ticker_means), 2)
                        if per_ticker_means else None),
                "플러스종목비율": (round(sum(1 for v in per_ticker_means if v > 0)
                                / len(per_ticker_means) * 100)
                             if per_ticker_means else None),
            }
        table.append(entry)
    table.sort(key=lambda e: -e["종목수"])
    return {"기준": label, "표": table}


def first_day_rows(graded):
    """종목이 그 라벨을 '처음 받은 날' 한 건씩만 골라낸다.

    머무는 내내를 평균낸 표와는 다른 질문에 답한다.
        머무는 내내  — 골든존 딱지가 붙어 있는 동안 아무 날이나 샀다면?
        최초 선정일  — 처음 골든존이 된 그날 샀다면?
    실제 매매에 가까운 쪽은 뒤엣것이다. 앞엣것보다 낫게 나오면
    선정 직후가 좋다는 뜻이고, 못하게 나오면 이미 오른 뒤에 걸린다는 뜻이다.

    같은 (종목, 라벨) 조합에서 가장 이른 날짜 하나만 남기므로
    경계선에서 라벨이 뒤집히는 종목도 최초 1회만 들어간다.
    """
    first = {}
    for row in graded:
        key = (row["티커"], row["라벨"])
        if key not in first or row["날짜"] < first[key]["날짜"]:
            first[key] = row
    return list(first.values())


def stability(records):
    """라벨이 얼마나 자주 뒤집히는지. 기준선이 흔들리는지 보는 진단."""
    by_ticker = defaultdict(list)
    for row in records:
        by_ticker[row["티커"]].append(row)

    rows = []
    for ticker, items in by_ticker.items():
        items.sort(key=lambda r: r["날짜"])
        changes = sum(1 for a, b in zip(items, items[1:]) if a["라벨"] != b["라벨"])
        days = (datetime.strptime(items[-1]["날짜"], "%Y-%m-%d")
                - datetime.strptime(items[0]["날짜"], "%Y-%m-%d")).days + 1
        per30 = changes / days * 30 if days else 0
        rows.append({"티커": ticker, "종목명": items[0]["종목명"],
                     "관측일수": len(items), "라벨변경": changes,
                     "30일당변경": round(per30, 1)})
    rows.sort(key=lambda r: -r["30일당변경"])
    unstable = [r for r in rows if r["30일당변경"] >= UNSTABLE_CHANGES]
    return rows, unstable


def band_of(score):
    if score is None:
        return None
    for name, low, high in SCORE_BANDS:
        if low <= score < high:
            return name
    return None


def render(section, lines):
    lines.append(f"\n### {section['기준']}\n")
    lines.append("| 구분 | 종목수 | 판정일수 | "
                 + " | ".join(f"{t} 초과(종목수)" for t, _ in INTERVALS) + " |")
    lines.append("|" + "---|" * (3 + len(INTERVALS)))
    for row in section["표"]:
        cells = []
        for tag, _ in INTERVALS:
            cell = row[tag]
            if cell["평균"] is None:
                cells.append("—")
            else:
                mark = " ⚠︎" if cell["종목수"] < MIN_TICKERS else ""
                cells.append(f"{cell['평균']:+.1f}% ({cell['종목수']}){mark}")
        lines.append(f"| {row['구분']} | {row['종목수']} | {row['판정일수']} | "
                     + " | ".join(cells) + " |")


def send_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20)
    except Exception as exc:                          # noqa: BLE001
        print(f"텔레그램 전송 실패: {exc}")


# ── 본체 ────────────────────────────────────────────────


def main():
    today = datetime.now(KST).date()
    records, legacy = load_records()

    if legacy:
        print(f"옛 형식 {legacy}줄은 티커가 없어 채점에서 제외했습니다.")
    if not records:
        print("채점할 판정이 없습니다. 파이프라인을 며칠 더 돌려 이력을 쌓아주세요.")
        return 0

    span = f"{records[0]['날짜']} ~ {records[-1]['날짜']}"
    tickers = {r["티커"] for r in records}
    print(f"판정 {len(records)}건 · 종목 {len(tickers)}개 ({span}) 채점 시작")

    graded, failed = grade(records, today)
    print(f"채점 완료 {len(graded)}건 · 시세 조회 실패 {len(failed)}종목")

    stab_rows, unstable = stability(records)

    firsts = first_day_rows(graded)

    sections = [
        summarize(graded, lambda r: r["라벨"],
                  "라벨별 ① 머무는 내내 — 딱지가 붙어 있는 동안 아무 날이나 샀다면"),
        summarize(firsts, lambda r: r["라벨"],
                  "라벨별 ② 최초 선정일 — 처음 그 라벨이 붙은 날 샀다면"),
        summarize(graded, lambda r: band_of(r.get("종합점수")), "종합점수 구간별"),
        summarize(graded, lambda r: band_of(r.get("내러티브점수")), "내러티브 점수 구간별"),
        summarize(graded, lambda r: band_of(r.get("펀더멘탈점수")), "펀더멘탈 점수 구간별"),
        summarize(graded, lambda r: "수급신호 있음" if r.get("수급신호") else "수급신호 없음",
                  "수급신호 유무별"),
    ]

    lines = ["# 엔진 자기평가",
             f"\n기준일 {today.isoformat()} (KST) · 판정 {len(graded)}건 · "
             f"종목 {len(tickers)}개 · 기간 {span}",
             "\n초과수익률은 코스피 대비입니다. 평균은 두 단계로 냅니다 — "
             "같은 라벨 안에서 종목별 평균을 먼저 내고, 그 값들을 다시 평균합니다. "
             "그래야 오래 머문 종목이나 경계선에서 판정이 뒤집히는 종목이 "
             "표본을 독차지하지 않습니다.",
             f"\n표본 단위는 종목 수입니다. {MIN_TICKERS}개 미만인 칸은 ⚠︎ 로 표시했습니다 — "
             "그 숫자로 가중치를 바꾸지 마세요."]
    for section in sections:
        render(section, lines)

    # 판정 안정성 — 기준선이 흔들리면 라벨별 비교 자체가 흐려진다.
    lines.append("\n### 판정 안정성 — 라벨이 자주 뒤집히는 종목\n")
    if unstable:
        lines.append(f"30일당 라벨 변경이 {UNSTABLE_CHANGES}회 이상인 종목 "
                     f"{len(unstable)}개 / 전체 {len(stab_rows)}개\n")
        lines.append("| 종목 | 관측일수 | 라벨변경 | 30일당 |")
        lines.append("|---|---|---|---|")
        for row in unstable[:15]:
            lines.append(f"| {row['종목명']} ({row['티커']}) | {row['관측일수']} | "
                         f"{row['라벨변경']} | {row['30일당변경']} |")
        lines.append("\n이 종목들은 점수가 기준선 바로 위아래에 있다는 뜻입니다. "
                     "수가 많아지면 엔진에 이력현상(한 번 골든존이 되면 "
                     "일정 점수 아래로 내려가야 풀리는 규칙)을 넣을 근거가 됩니다.")
    else:
        lines.append("라벨이 자주 뒤집히는 종목은 없습니다.")

    if failed:
        lines.append(f"\n### 채점 못 한 종목 {len(failed)}개\n")
        for ticker, name, why in failed[:20]:
            lines.append(f"- {name} ({ticker}) — {why}")

    lines.append("\n---\n"
                 "**①과 ②를 나란히 보는 법.** ②(최초 선정일)가 ①(머무는 내내)보다 높으면 "
                 "신호가 붙은 직후가 가장 좋다는 뜻이고, 알림을 받은 날 움직이는 것이 유리합니다. "
                 "반대로 ②가 낮으면 이미 오른 뒤에 신호가 붙는다는 뜻이라, "
                 "엔진이 뒤늦게 따라가고 있는 것입니다. 그때는 내러티브 쪽 배점을 "
                 "줄이거나 최신성 가중치를 넣을 근거가 됩니다. "
                 "②는 종목당 한 건뿐이라 표본이 훨씬 천천히 쌓입니다.\n")
    lines.append("**읽는 법.** 라벨별 표에서 골든존이 관망보다 꾸준히 높아야 "
                 "엔진에 판별력이 있는 것입니다. 점수 구간별 표에서 "
                 "점수가 높을수록 초과수익률이 높아지는 단조 관계가 보이면 그 축은 유효하고, "
                 "뒤섞여 있으면 그 축의 배점을 줄일 근거가 됩니다. "
                 "내러티브와 펀더멘탈을 따로 본 이유가 그것입니다.")

    report = "\n".join(lines)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    with open(EVAL_FILE, "w", encoding="utf-8") as f:
        json.dump({"기준일": today.isoformat(), "판정수": len(graded),
                   "종목수": len(tickers), "기간": span, "집계": sections,
                   "판정안정성": stab_rows,
                   "채점실패": [{"티커": t, "종목명": n, "사유": w} for t, n, w in failed]},
                  f, ensure_ascii=False, indent=2)

    print("\n" + report)

    label_table = sections[0]["표"]
    if label_table and label_table[0]["1개월"]["종목수"] >= MIN_TICKERS:
        head = label_table[0]
        send_telegram(f"📊 <b>엔진 자기평가</b> ({today})\n"
                      f"판정 {len(graded)}건 · 종목 {len(tickers)}개 · 기간 {span}\n"
                      f"{head['구분']} 종목 {head['종목수']}개 · "
                      f"1개월 초과 {head['1개월']['평균']}%\n"
                      f"자세한 표는 data/engine_eval.md")
    else:
        print(f"\n아직 종목 수가 {MIN_TICKERS}개에 못 미쳐 텔레그램 요약은 보내지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
