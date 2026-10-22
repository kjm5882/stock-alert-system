# ============================================================
# 티앤엘(340570) 핵심 지표 이벤트 알림
# 매일 DART를 확인해 아래 공시가 새로 뜬 경우에만 텔레그램으로 알린다.
#  - 분기/반기/사업보고서: 매출·영업이익·영업이익률·전년 동기 대비(YoY) 요약
#  - 시설투자·단일판매·공급계약 공시: 제목+링크
#  - C&D 분기 실적 발표 확인일(REMINDER_DATES)에 1회 리마인더
# 기존 watchlist_monitor와 별개로 동작하며 상태는 data/tnl_seen.json 에 저장한다.
# ============================================================
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dart_client, telegram_notify, prelim_earnings
import re

TICKER = "340570"
NAME = "티앤엘"
STATE_PATH = "data/tnl_seen.json"
EVENT_KEYWORDS = ["신규시설투자", "시설투자", "단일판매", "공급계약"]
# C&D 3분기 발표(통상 10월 말~11월 초)를 확인할 날짜. 발표일이 확정되면 수정.
REMINDER_DATES = {"2026-11-02": "C&D 2026 3분기 실적 발표 확인 (Hero 언급·성장·점유율, 제2 공급처 언급 여부)"}
KST = timezone(timedelta(hours=9))


def parse_report_period(report_nm, rcept_dt):
    m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
    year, month = (m.group(1), m.group(2)) if m else (rcept_dt[:4], rcept_dt[4:6])
    if "사업보고서" in report_nm:
        return year, "11011"
    if "반기보고서" in report_nm:
        return year, "11012"
    return year, ("11014" if month == "09" else "11013")


def load():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"processed": [], "reminders": []}


def save(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def get_accounts(corp_code, year, code):
    st = dart_client.get_financial_statement(corp_code, year, code) or \
        dart_client.get_financial_statement(corp_code, year, code, fs_div="OFS")
    return dart_client.extract_key_accounts(st) if st else {}


def eok(v):
    return f"{v / 1e8:,.0f}억" if v is not None else "N/A"


def periodic_summary(corp_code, d):
    year, code = parse_report_period(d.get("report_nm", ""), d.get("rcept_dt", ""))
    cur = get_accounts(corp_code, year, code)
    prev = get_accounts(corp_code, str(int(year) - 1), code)
    sales, op = cur.get("매출액"), cur.get("영업이익")
    lines = [f"📗 {NAME}({TICKER}) {d.get('report_nm', '')}"]
    lines.append(f"  매출액: {eok(sales)} / 영업이익: {eok(op)}")
    if sales and op is not None:
        lines.append(f"  영업이익률: {op / sales * 100:.1f}%")
    ps, po = prev.get("매출액"), prev.get("영업이익")
    if sales and ps:
        lines.append(f"  매출 YoY: {(sales / ps - 1) * 100:+.1f}%")
    if op is not None and po:
        lines.append(f"  영업이익 YoY: {(op / po - 1) * 100:+.1f}%")
    lines.append("  ➡ 점검: 최대 매출처(C社) 비중은 원문 'II.사업의 내용 > 주요 매출처'에서 직접 확인")
    lines.append("  * 연결 기준·해당 보고서 '당기' 금액. 반기·사업보고서는 기간 누적일 수 있어 원문 확인")
    lines.append(f"\n원문: {prelim_earnings.build_viewer_url(d.get('rcept_no', ''))}")
    return "\n".join(lines)


def run():
    s = load()
    today = datetime.now(KST)
    bgn, end = (today - timedelta(days=5)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
    corp_code = dart_client.get_corp_code(TICKER)
    if not corp_code:
        telegram_notify.send_message(f"⚠️ {NAME}({TICKER}): DART corp_code를 찾지 못했습니다.")
        return
    changed = False
    seen = set(s["processed"])

    items = dart_client.get_recent_disclosures(corp_code, bgn, end)
    for d in items:
        if d["kind"] == "periodic" and d["rcept_no"] not in seen:
            try:
                telegram_notify.send_message(periodic_summary(corp_code, d))
            except Exception as e:
                telegram_notify.send_message(f"❌ {NAME} 정기보고서 요약 실패({e})\n원문: {prelim_earnings.build_viewer_url(d['rcept_no'])}")
            seen.add(d["rcept_no"]); changed = True
        elif d["kind"] == "prelim" and d["rcept_no"] not in seen:
            telegram_notify.send_message(f"⚡ {NAME} {d.get('report_nm', '')}\n원문: {prelim_earnings.build_viewer_url(d['rcept_no'])}")
            seen.add(d["rcept_no"]); changed = True

    # 시설투자·공급계약 등 주요사항/거래소공시 (유형 B, I 전체 조회 후 제목 필터)
    for ty in ("B", "I"):
        for d in dart_client._search_disclosures(corp_code, bgn, end, pblntf_ty=ty):
            if d["rcept_no"] in seen or not any(k in d.get("report_nm", "") for k in EVENT_KEYWORDS):
                continue
            telegram_notify.send_message(f"🏭 {NAME}({TICKER}) 공시: {d['report_nm']}\n원문: {prelim_earnings.build_viewer_url(d['rcept_no'])}")
            seen.add(d["rcept_no"]); changed = True

    key = today.strftime("%Y-%m-%d")
    if key in REMINDER_DATES and key not in s["reminders"]:
        telegram_notify.send_message(f"🔔 {NAME} 점검 일정: {REMINDER_DATES[key]}")
        s["reminders"].append(key); changed = True

    if changed:
        s["processed"] = sorted(seen)[-50:]
        save(s)
    else:
        print("새 이벤트 없음 (정상)")


if __name__ == "__main__":
    run()
