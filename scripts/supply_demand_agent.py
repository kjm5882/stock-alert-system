"""
수급 담당자 (Supply-Demand Agent) v2 - 네이버 API 기반
====================================================
변경 이유: KRX가 GitHub Actions IP를 차단하여 pykrx의 KRX 계열 함수가 모두 실패.
         네이버 금융이 Next.js로 개편되며 HTML 표도 사라짐.
         → 네이버 신규 JSON API로 전환 (pykrx 의존 완전 제거).

사용 API:
  - 수급: https://m.stock.naver.com/api/stock/{코드}/trend  (최근 10거래일)
  - 종목명: https://m.stock.naver.com/api/stock/{코드}/basic

동작:
  1. data/feed_signals.jsonl 에서 최근 N일간 언급된 종목명 수집
  2. 종목명 → 티커 매핑 (data/ticker_name_map.json 사전 이용)
  3. 각 티커의 최근 수급 데이터 조회
  4. 패턴 감지: 연속 순매수 3일 이상 / 평소 대비 3배 이상 급증
  5. 저장 + 텔레그램 알림

주의: 네이버 API 구조가 바뀌면 조용히 실패할 수 있으므로,
     데이터를 전혀 못 가져오면 텔레그램으로 경고를 보냅니다.
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone

import requests

# ── 설정 ──────────────────────────────────────────────
LOOKBACK_DAYS_FOR_MENTIONS = 14     # 피드에서 며칠치 언급을 볼지
CONSECUTIVE_DAYS_THRESHOLD = 3      # 연속 순매수 며칠부터 신호로 볼지
SPIKE_MULTIPLIER_THRESHOLD = 3.0    # 평소 대비 몇 배부터 급증으로 볼지

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FEED_SIGNALS_FILE = os.path.join(DATA_DIR, "feed_signals.jsonl")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_name_map.json")
SEEN_FILE = os.path.join(DATA_DIR, "supply_demand_seen.json")
SIGNALS_FILE = os.path.join(DATA_DIR, "supply_demand_signals.jsonl")
UNMATCHED_FILE = os.path.join(DATA_DIR, "supply_demand_unmatched.json")

NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 블로그/유튜브에서 흔히 쓰이는 약칭 → 정식 종목명 보정
ALIAS_MAP = {
    "콜마": "한국콜마",
    "코스메카": "코스메카코리아",
    "SKT": "SK텔레콤",
    "네이버": "NAVER",
    "삼성SDI": "삼성SDI",
}


# ── 파일 입출력 ──────────────────────────────────────
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


def append_signal(record):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SIGNALS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── 1) 최근 언급 종목 추출 ───────────────────────────
def get_recently_mentioned_stocks():
    """feed_signals.jsonl 에서 최근 N일간 언급된 종목명 집합."""
    if not os.path.exists(FEED_SIGNALS_FILE):
        print("[알림] feed_signals.jsonl 이 없습니다. 피드 담당자를 먼저 실행하세요.")
        return set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS_FOR_MENTIONS)
    names = set()

    with open(FEED_SIGNALS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["timestamp"])
                if ts >= cutoff and rec.get("종목명"):
                    names.add(rec["종목명"].strip())
            except Exception:
                continue
    return names


# ── 2) 종목명 → 티커 매핑 ────────────────────────────
def match_stock_name_to_ticker(stock_name, ticker_map):
    """저장된 매핑 사전에서 종목명을 티커로 변환."""
    if stock_name in ticker_map:
        return ticker_map[stock_name]

    if stock_name in ALIAS_MAP and ALIAS_MAP[stock_name] in ticker_map:
        return ticker_map[ALIAS_MAP[stock_name]]

    normalized = stock_name.replace("(주)", "").replace("주식회사", "").strip()
    if normalized in ticker_map:
        return ticker_map[normalized]

    return None


def verify_ticker_name(ticker):
    """네이버 basic API로 티커의 실제 종목명을 확인 (검증용)."""
    url = f"https://m.stock.naver.com/api/stock/{ticker}/basic"
    try:
        r = requests.get(url, headers=NAVER_HEADERS, timeout=15)
        if r.status_code == 200:
            return r.json().get("stockName")
    except Exception:
        pass
    return None


# ── 3) 수급 데이터 조회 및 패턴 분석 ─────────────────
def parse_number(text):
    """'+1,379,866' / '-1,088,039' 형태 문자열을 정수로 변환."""
    if text is None:
        return 0
    cleaned = str(text).replace(",", "").replace("+", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return 0


def fetch_trend(ticker):
    """네이버 trend API에서 최근 10거래일 수급 데이터를 가져온다."""
    url = f"https://m.stock.naver.com/api/stock/{ticker}/trend"
    try:
        r = requests.get(url, headers=NAVER_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"  [수급 조회 실패] {ticker}: 상태코드 {r.status_code}")
            return None
        data = r.json()
        if not isinstance(data, list) or not data:
            print(f"  [수급 데이터 없음] {ticker}")
            return None
        return data
    except Exception as e:
        print(f"  [수급 조회 오류] {ticker}: {e}")
        return None


def analyze_supply_demand(trend_data):
    """수급 데이터로 연속순매수 / 급증 패턴을 분석.
    trend_data는 최신일이 첫 번째(내림차순)로 들어온다."""
    result = {}

    investors = {
        "외국인": "foreignerPureBuyQuant",
        "기관": "organPureBuyQuant",
    }

    # 최신 종가 (금액 환산용)
    close_price = parse_number(trend_data[0].get("closePrice"))

    for label, key in investors.items():
        values = [parse_number(d.get(key)) for d in trend_data]  # 최신 → 과거 순

        # 연속 순매수일수 (최신일부터)
        consecutive = 0
        for v in values:
            if v > 0:
                consecutive += 1
            else:
                break

        # 급증 배수 (최신일 vs 그 이전 평균, 절대값 기준)
        today_value = values[0] if values else 0
        prior = [abs(v) for v in values[1:]]
        prior_avg = sum(prior) / len(prior) if prior else 0
        spike_ratio = (today_value / prior_avg) if prior_avg > 0 else 0

        result[label] = {
            "연속순매수일수": consecutive,
            "오늘순매수수량": today_value,
            "오늘순매수금액_추정": today_value * close_price,
            "급증배수": round(spike_ratio, 1),
        }

    result["_기준일"] = trend_data[0].get("bizdate")
    result["_종가"] = close_price
    return result


# ── 텔레그램 ─────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[텔레그램 미설정] 전송 생략")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=15)
    except Exception as e:
        print(f"[텔레그램 오류] {e}")


def format_amount(won):
    """원 단위를 읽기 쉬운 한글 단위로."""
    if abs(won) >= 1_0000_0000:
        return f"{won / 1_0000_0000:,.1f}억"
    if abs(won) >= 1_0000:
        return f"{won / 1_0000:,.0f}만"
    return f"{won:,}"


# ── 메인 ─────────────────────────────────────────────
def main():
    mentioned = get_recently_mentioned_stocks()
    print(f"최근 {LOOKBACK_DAYS_FOR_MENTIONS}일간 언급된 종목: {len(mentioned)}개")
    if not mentioned:
        print("확인할 종목이 없습니다. 종료합니다.")
        return

    ticker_map = load_json(TICKER_MAP_FILE, {})
    if not ticker_map:
        msg = ("⚠️ <b>수급 담당자 경고</b>\n"
               "종목명↔티커 매핑 파일(data/ticker_name_map.json)이 비어 있습니다.\n"
               "매핑 파일을 먼저 생성해야 수급 확인이 가능합니다.")
        print(msg)
        send_telegram(msg)
        return

    seen = load_json(SEEN_FILE, {})
    unmatched = load_json(UNMATCHED_FILE, [])
    today_str = datetime.now().strftime("%Y-%m-%d")

    new_signals = []
    fetch_success = 0
    fetch_attempt = 0

    for name in sorted(mentioned):
        ticker = match_stock_name_to_ticker(name, ticker_map)
        if not ticker:
            if name not in unmatched:
                unmatched.append(name)
            print(f"  [미매칭] {name}")
            continue

        seen_key = f"{ticker}_{today_str}"
        if seen_key in seen:
            continue

        print(f"  분석 중: {name} ({ticker})")
        fetch_attempt += 1
        trend = fetch_trend(ticker)
        seen[seen_key] = True
        time.sleep(0.4)

        if not trend:
            continue
        fetch_success += 1

        analysis = analyze_supply_demand(trend)

        reasons = []
        for label in ["외국인", "기관"]:
            stats = analysis[label]
            if stats["연속순매수일수"] >= CONSECUTIVE_DAYS_THRESHOLD:
                reasons.append(f"{label} {stats['연속순매수일수']}일 연속 순매수")
            if stats["급증배수"] >= SPIKE_MULTIPLIER_THRESHOLD:
                reasons.append(
                    f"{label} 순매수 평소 대비 {stats['급증배수']}배 급증 "
                    f"({format_amount(stats['오늘순매수금액_추정'])}원)"
                )

        if reasons:
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "종목명": name,
                "티커": ticker,
                "기준일": analysis["_기준일"],
                "신호": reasons,
                "상세": {k: v for k, v in analysis.items() if not k.startswith("_")},
            }
            append_signal(record)
            new_signals.append(record)

    save_json(SEEN_FILE, seen)
    save_json(UNMATCHED_FILE, unmatched)

    # 파이프라인 건강 체크: 시도했는데 전부 실패하면 구조 변경 의심
    if fetch_attempt > 0 and fetch_success == 0:
        send_telegram(
            "⚠️ <b>수급 담당자 경고</b>\n"
            f"{fetch_attempt}개 종목 조회를 시도했으나 모두 실패했습니다.\n"
            "네이버 API 구조가 변경되었을 수 있습니다."
        )
        print("\n[경고] 모든 조회 실패 - API 구조 변경 의심")
        return

    if new_signals:
        lines = [f"💹 <b>수급 담당자 알림</b> ({len(new_signals)}건)\n"]
        for r in new_signals:
            lines.append(f"🔹 <b>{r['종목명']}</b> ({r['티커']})")
            for reason in r["신호"]:
                lines.append(f"   └ {reason}")
        send_telegram("\n".join(lines))
        print(f"\n총 {len(new_signals)}건의 수급 신호 → 텔레그램 전송 완료")
    else:
        print(f"\n조회 성공 {fetch_success}개 종목, 특이 수급 신호는 없습니다.")

    if unmatched:
        print(f"[참고] 미매칭 종목명 {len(unmatched)}개 "
              f"(data/supply_demand_unmatched.json 확인)")


if __name__ == "__main__":
    main()
