#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
보유종목 공시 감시 — 내가 들고 있는 종목에 중요한 공시가 뜨면 바로 알린다.

왜 따로 만드나
  src/watchlist_monitor.py 는 config/dashboard_stocks.py 의 반도체 중소형주
  20종목을 대상으로 '정기보고서·잠정실적'만 본다. 대시보드용이다.
  이 스크립트는 **내가 실제로 사고파는 종목**이 대상이고, 주가를 움직이는
  공시 전반(수주·증자·자사주·최대주주변경·소송 등)을 본다. 서로 건드리지 않는다.

왜 데이터 파일인가
  매매하면 목록이 바뀐다. 코드를 고치게 만들면 안 바꾸게 된다.
  data/watchlist.json 만 손보면 된다.

노이즈를 막는 장치 (일일 알림을 껐던 이유를 여기서 반복하지 않기 위해)
  1. 화이트리스트 — 중요 유형만 본다. 기업설명회 개최·주주명부 폐쇄 따위는 아예 제외.
  2. 접수번호(rcept_no) 기준 중복 제거 — 같은 공시는 평생 한 번만.
  3. 정정 공시는 원본과 묶어 최신본만.
  4. 한 번에 보낼 상한 — 넘치면 건수만 알리고 끊는다.

실행:  python scripts/watchlist_alert.py
환경:  DART_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO

import requests

KST = timezone(timedelta(hours=9))

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
SEEN_FILE = os.path.join(DATA_DIR, "watchlist_seen.json")
CORP_CODE_CACHE = os.path.join(DATA_DIR, "corp_codes.xml")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_name_map.json")

DART_API_KEY = os.environ.get("DART_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

LOOKBACK_DAYS = 3          # 조회 창. 주말·휴일을 건너뛰어도 놓치지 않게 넉넉히.
SEEN_KEEP_DAYS = 90        # 본 공시 기록 보관 기간
MAX_ALERTS_PER_RUN = 25    # 한 번에 보낼 상한
REQUEST_PAUSE = 0.15       # DART 호출 간격

# ── 중요 공시 (이것만 알린다) ────────────────────────
# 키워드는 report_nm(공시명)에 대한 부분일치다.
# 순서가 곧 우선순위 — 먼저 맞는 분류로 잡는다.
IMPORTANT = [
    ("수주",       ["단일판매", "공급계약"]),
    ("실적",       ["영업(잠정)실적", "잠정실적", "매출액또는손익구조", "손익구조"]),
    ("희석",       ["유상증자결정", "전환사채권발행결정", "신주인수권부사채권발행결정",
                   "교환사채권발행결정", "감자결정", "무상증자결정"]),
    ("지배구조",   ["최대주주변경", "대량보유상황보고서", "경영권", "공개매수",
                   "의결권대리행사권유"]),
    ("내부자",     ["임원ㆍ주요주주특정증권등소유상황보고서",
                   "임원·주요주주특정증권등소유상황보고서"]),
    ("주주환원",   ["자기주식취득", "자기주식처분", "자기주식소각", "현금ㆍ현물배당",
                   "현금·현물배당", "신탁계약"]),
    ("구조변경",   ["합병결정", "분할결정", "영업양수", "영업양도",
                   "주식교환", "타법인주식및출자증권"]),
    ("사고",       ["소송", "횡령", "배임", "파산", "회생절차", "감사의견",
                   "거래정지", "관리종목", "상장폐지", "불성실공시"]),
]

# 정기보고서는 기본으로 끈다 — src/watchlist_monitor.py 가 이미 알린다.
# 그 워크플로우를 끄실 거면 watchlist.json 에서 "정기보고서알림": true 로.
PERIODIC = ["사업보고서", "반기보고서", "분기보고서"]

# 제목에 이게 들어가면 중요 유형이라도 버린다.
NOISE = ["기업설명회", "주주명부폐쇄", "결산실적공시예고", "안내공시",
         "조회공시요구", "풍문또는보도"]


# ── 유틸 ────────────────────────────────────────────
def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                     # noqa: BLE001
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# ── 보유종목 읽기 ────────────────────────────────────
def load_watchlist() -> tuple[list[dict], bool]:
    """watchlist.json 을 읽어 [{종목명, 티커, 메모}] 로 돌려준다.

    형식을 너그럽게 받는다 — 급할 때 손으로 고칠 파일이기 때문이다.
      {"종목": [{"종목명": "...", "티커": "045100"}, ...]}
      {"종목": ["한양이엔지", "045100", ...]}
      ["한양이엔지", ...]
    티커가 없으면 ticker_name_map.json 으로 채운다.
    """
    raw = load_json(WATCHLIST_FILE, None)
    if raw is None:
        return [], False

    periodic = False
    if isinstance(raw, dict):
        periodic = bool(raw.get("정기보고서알림"))
        items = raw.get("종목") or raw.get("watchlist") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return [], False

    name_to_ticker = {}
    for name, tk in (load_json(TICKER_MAP_FILE, {}) or {}).items():
        name_to_ticker[str(name).strip()] = str(tk).strip()

    out, seen_tk = [], set()
    for it in items:
        if isinstance(it, str):
            s = it.strip()
            if re.fullmatch(r"\d{6}", s):
                entry = {"종목명": "", "티커": s, "메모": ""}
            else:
                entry = {"종목명": s, "티커": name_to_ticker.get(s, ""), "메모": ""}
        elif isinstance(it, dict):
            entry = {"종목명": str(it.get("종목명") or it.get("name") or "").strip(),
                     "티커": str(it.get("티커") or it.get("ticker") or "").strip(),
                     "메모": str(it.get("메모") or it.get("note") or "").strip()}
            if not entry["티커"] and entry["종목명"]:
                entry["티커"] = name_to_ticker.get(entry["종목명"], "")
        else:
            continue

        if not entry["티커"]:
            log(f"  [티커 미상] {entry['종목명']} — watchlist.json 에 티커를 적어주세요")
            continue
        if entry["티커"] in seen_tk:
            continue
        seen_tk.add(entry["티커"])
        out.append(entry)
    return out, periodic


# ── DART 고유번호 ────────────────────────────────────
def load_corp_map() -> dict:
    """stock_code(6자리) → corp_code(8자리). 캐시가 없으면 받아서 저장한다."""
    if not os.path.exists(CORP_CODE_CACHE):
        log("  corp_codes.xml 없음 — DART에서 받습니다")
        try:
            r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                             params={"crtfc_key": DART_API_KEY}, timeout=60)
            r.raise_for_status()
            with zipfile.ZipFile(BytesIO(r.content)) as z:
                name = z.namelist()[0]
                os.makedirs(os.path.dirname(CORP_CODE_CACHE), exist_ok=True)
                with open(CORP_CODE_CACHE, "wb") as f:
                    f.write(z.read(name))
        except Exception as exc:                          # noqa: BLE001
            log(f"  [고유번호 내려받기 실패] {str(exc)[:90]}")
            return {}

    try:
        root = ET.parse(CORP_CODE_CACHE).getroot()
    except Exception as exc:                              # noqa: BLE001
        log(f"  [corp_codes.xml 파싱 실패] {str(exc)[:90]}")
        return {}

    mapping = {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if stock and corp and stock != " ":
            mapping[stock] = corp
    return mapping


# ── 공시 조회 ────────────────────────────────────────
def fetch_filings(corp_code: str, bgn: str, end: str) -> list[dict]:
    """한 회사의 기간 내 공시 목록. 실패/없음은 빈 리스트."""
    try:
        r = requests.get("https://opendart.fss.or.kr/api/list.json",
                         params={"crtfc_key": DART_API_KEY, "corp_code": corp_code,
                                 "bgn_de": bgn, "end_de": end,
                                 "page_count": "100"}, timeout=20)
        if r.status_code != 200:
            log(f"  [공시 조회 실패] {corp_code}: 상태코드 {r.status_code}")
            return []
        data = r.json()
    except Exception as exc:                              # noqa: BLE001
        log(f"  [공시 조회 오류] {corp_code}: {str(exc)[:70]}")
        return []

    status = str(data.get("status", ""))
    if status == "013":          # 조회된 데이터 없음 — 정상이다
        return []
    if status != "000":
        log(f"  [공시 조회 거절] {corp_code}: {status} {data.get('message', '')[:50]}")
        return []
    rows = data.get("list")
    return rows if isinstance(rows, list) else []


def classify(report_nm: str, allow_periodic: bool) -> str | None:
    """공시명을 보고 분류를 돌려준다. 중요하지 않으면 None."""
    nm = (report_nm or "").replace(" ", "")
    if any(bad.replace(" ", "") in nm for bad in NOISE):
        return None
    for label, keys in IMPORTANT:
        if any(k.replace(" ", "") in nm for k in keys):
            return label
    if allow_periodic and any(k in nm for k in PERIODIC):
        return "정기보고서"
    return None


# ── 중복 정리 ────────────────────────────────────────
def prune_seen(seen: dict, today: datetime) -> dict:
    """오래된 접수번호는 버린다. 파일이 무한히 자라지 않게."""
    cutoff = (today - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y%m%d")
    return {k: v for k, v in seen.items() if str(v)[:8] >= cutoff}


def dedupe_corrections(items: list[dict]) -> list[dict]:
    """같은 공시의 정정본이 여러 건이면 최신 것만 남긴다.

    '[기재정정]단일판매ㆍ공급계약체결' 과 '단일판매ㆍ공급계약체결' 은
    제목 앞의 대괄호만 다르다. 그 둘을 한 건으로 본다.
    """
    best = {}
    for it in items:
        base = re.sub(r"^\[[^\]]*\]\s*", "", it["공시명"]).replace(" ", "")
        key = (it["티커"], base)
        old = best.get(key)
        if old is None or it["접수번호"] > old["접수번호"]:
            best[key] = it
    return sorted(best.values(), key=lambda x: x["접수번호"], reverse=True)


# ── 텔레그램 ─────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("[텔레그램 미설정] 전송 생략")
        log(re.sub(r"<[^>]+>", "", message))
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15)
    except Exception as exc:                              # noqa: BLE001
        log(f"[텔레그램 오류] {str(exc)[:90]}")


ICON = {"수주": "📄", "실적": "📊", "희석": "💧", "지배구조": "👤",
        "내부자": "🧾", "주주환원": "💰", "구조변경": "🔀",
        "사고": "⚠️", "정기보고서": "📘"}


def build_message(found: list[dict], total: int) -> str:
    lines = [f"🔔 <b>보유종목 공시</b> ({total}건)\n"]
    by_stock = {}
    for it in found:
        by_stock.setdefault((it["종목명"], it["티커"], it["메모"]), []).append(it)

    for (name, tk, memo), items in by_stock.items():
        head = f"🔹 <b>{name or tk}</b> ({tk})"
        if memo:
            head += f" <i>{memo}</i>"
        lines.append(head)
        for it in items:
            icon = ICON.get(it["분류"], "•")
            lines.append(f"   {icon} [{it['분류']}] {it['공시명']}")
            lines.append(f"      {it['날짜']} · {it['제출인']}")
            lines.append(f"      https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['접수번호']}")
    if total > len(found):
        lines.append(f"\n<i>※ {total - len(found)}건은 많아서 생략했습니다. "
                     f"DART에서 확인해 주세요.</i>")
    return "\n".join(lines)


# ── 메인 ─────────────────────────────────────────────
def main():
    if not DART_API_KEY:
        log("DART_API_KEY 가 없습니다. 종료합니다.")
        return 1

    watchlist, allow_periodic = load_watchlist()
    if not watchlist:
        log(f"보유종목이 없습니다 — {WATCHLIST_FILE} 를 만들어 주세요.")
        log('예: {"종목": [{"종목명": "한양이엔지", "티커": "045100", "메모": "보유"}]}')
        return 0

    now = datetime.now(KST)
    bgn = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    log(f"보유종목 {len(watchlist)}개 · 조회 {bgn}~{end} (KST {now:%Y-%m-%d %H:%M})")
    log(f"정기보고서 알림: {'켜짐' if allow_periodic else '꺼짐 (src/watchlist_monitor.py 담당)'}")

    corp_map = load_corp_map()
    if not corp_map:
        send_telegram("⚠️ <b>보유종목 감시 경고</b>\n"
                      "DART 고유번호 매핑을 읽지 못했습니다.")
        return 1

    seen = prune_seen(load_json(SEEN_FILE, {}), now)
    before = len(seen)

    found, checked, missing = [], 0, []
    for st in watchlist:
        corp = corp_map.get(st["티커"])
        if not corp:
            missing.append(f"{st['종목명'] or ''}({st['티커']})")
            continue
        checked += 1
        for row in fetch_filings(corp, bgn, end):
            rcept = str(row.get("rcept_no") or "").strip()
            if not rcept or rcept in seen:
                continue
            label = classify(row.get("report_nm"), allow_periodic)
            if not label:
                continue
            found.append({
                "종목명": st["종목명"] or str(row.get("corp_name") or ""),
                "티커": st["티커"], "메모": st["메모"], "분류": label,
                "공시명": str(row.get("report_nm") or "").strip(),
                "제출인": str(row.get("flr_nm") or "").strip(),
                "날짜": str(row.get("rcept_dt") or "").strip(),
                "접수번호": rcept,
            })
        time.sleep(REQUEST_PAUSE)

    # 조회는 됐는데 전부 실패하면 구조 변경을 의심해야 한다.
    if checked == 0:
        send_telegram("⚠️ <b>보유종목 감시 경고</b>\n"
                      "보유종목 중 DART 고유번호가 잡히는 종목이 없습니다.\n"
                      "티커를 확인해 주세요.")
        log("[경고] 고유번호가 잡힌 종목 0개")
        return 1

    uniq = dedupe_corrections(found)

    if uniq:
        send_telegram(build_message(uniq[:MAX_ALERTS_PER_RUN], len(uniq)))
        log(f"\n신규 중요 공시 {len(uniq)}건 → 텔레그램 전송")
        for it in uniq:
            log(f"  [{it['분류']}] {it['종목명']}({it['티커']}) {it['공시명']}")
    else:
        log("\n신규 중요 공시 없음")

    # 본 것은 분류와 무관하게 전부 기록한다 —
    # 중요하지 않다고 버리면 다음 실행에서 또 들여다보게 된다.
    for it in found:
        seen[it["접수번호"]] = it["날짜"] or end
    save_json(SEEN_FILE, seen)

    log(f"\n조회 {checked}/{len(watchlist)}종목 · "
        f"기록 {before} → {len(seen)}건")
    if missing:
        log(f"[참고] DART 고유번호 미상 {len(missing)}개 — {', '.join(missing[:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
