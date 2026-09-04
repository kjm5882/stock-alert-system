"""
수급 담당자 (Supply-Demand Agent)
================================
목적: 피드 담당자가 감지한 종목들 대상으로, 외국인/기관 순매수 흐름에
     의미있는 패턴(연속 순매수, 급증)이 있는지 확인해 알림.

동작:
  1. data/feed_signals.jsonl 에서 최근 N일간 언급된 종목명을 모은다.
  2. 종목명 → 티커 매핑 (pykrx 전체 종목 리스트 캐시 활용).
  3. 매칭된 티커별로 최근 15거래일 외국인/기관 순매수 대금을 가져온다.
  4. 패턴 감지:
     - 연속 순매수일수 >= 3일
     - 또는 순매수 급증(최근 10일 평균 대비 3배 이상)
  5. 결과 저장 + 텔레그램 알림.

주의: 종목명 매칭이 안 되는 경우 data/supply_demand_unmatched.json 에
     쌓이니, 주기적으로 확인해서 종목명 표기가 특이한 케이스를 보완하면 됨.
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from pykrx import stock

# ── 설정 ──────────────────────────────────────────────
LOOKBACK_DAYS_FOR_MENTIONS = 7      # 피드에서 며칠치 언급을 볼지
TRADING_DAYS_FOR_SUPPLY = 15        # 수급 데이터 몇 거래일치 볼지
CONSECUTIVE_DAYS_THRESHOLD = 3      # 연속 순매수 며칠부터 신호로 볼지
SPIKE_MULTIPLIER_THRESHOLD = 3.0    # 평소 대비 몇 배부터 급증으로 볼지

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FEED_SIGNALS_FILE = os.path.join(DATA_DIR, "feed_signals.jsonl")
TICKER_MAP_FILE = os.path.join(DATA_DIR, "ticker_name_map.json")
SEEN_FILE = os.path.join(DATA_DIR, "supply_demand_seen.json")
SIGNALS_FILE = os.path.join(DATA_DIR, "supply_demand_signals.jsonl")
UNMATCHED_FILE = os.path.join(DATA_DIR, "supply_demand_unmatched.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ── 유틸: 파일 입출력 ────────────────────────────────
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
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
    """feed_signals.jsonl 에서 최근 N일간 언급된 종목명 집합을 만든다."""
    if not os.path.exists(FEED_SIGNALS_FILE):
        print("[알림] feed_signals.jsonl 이 아직 없습니다. 피드 담당자를 먼저 실행하세요.")
        return set()

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS_FOR_MENTIONS)
    stock_names = set()

    with open(FEED_SIGNALS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                ts = datetime.fromisoformat(record["timestamp"])
                if ts >= cutoff and record.get("종목명"):
                    stock_names.add(record["종목명"].strip())
            except Exception:
                continue

    return stock_names


# ── 2) 종목명 → 티커 매핑 ────────────────────────────
def build_or_load_ticker_map():
    """전체 종목명-티커 매핑을 만들거나 캐시에서 불러온다 (7일마다 갱신)."""
    if os.path.exists(TICKER_MAP_FILE):
        mtime = datetime.fromtimestamp(os.path.getmtime(TICKER_MAP_FILE), tz=timezone.utc)
        if datetime.now(timezone.utc) - mtime < timedelta(days=7):
            return load_json(TICKER_MAP_FILE, {})

    print("[티커맵] 새로 생성 중... (KOSPI+KOSDAQ 전체 종목)")
    name_to_ticker = {}
    today = datetime.now().strftime("%Y%m%d")

    for market in ["KOSPI", "KOSDAQ"]:
        tickers = stock.get_market_ticker_list(today, market=market)
        for ticker in tickers:
            try:
                name = stock.get_market_ticker_name(ticker)
                name_to_ticker[name] = ticker
            except Exception:
                continue

    save_json(TICKER_MAP_FILE, name_to_ticker)
    print(f"[티커맵] {len(name_to_ticker)}개 종목 매핑 완료")
    return name_to_ticker


def match_stock_name_to_ticker(stock_name, ticker_map):
    """종목명을 티커로 매칭. 정확히 일치 안 하면 살짝 정규화해서 재시도."""
    if stock_name in ticker_map:
        return ticker_map[stock_name]

    # 흔한 접미사/공백 정리 후 재시도
    normalized = stock_name.replace("(주)", "").replace("주식회사", "").strip()
    for name, ticker in ticker_map.items():
        if normalized == name.strip():
            return ticker

    # 부분 포함 매칭 (예: "삼성전자우선주" 같은 케이스는 걸러질 수 있음 - 후보만 로그)
    candidates = [name for name in ticker_map if normalized in name or name in normalized]
    if len(candidates) == 1:
        return ticker_map[candidates[0]]

    return None


# ── 3) 수급 데이터 조회 및 패턴 분석 ─────────────────
def analyze_supply_demand(ticker):
    """최근 N거래일 외국인/기관 순매수 데이터로 연속매수/급증 패턴을 분석."""
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=TRADING_DAYS_FOR_SUPPLY * 2)).strftime("%Y%m%d")

    try:
        df = stock.get_market_trading_value_by_date(start_date, end_date, ticker)
    except Exception as e:
        print(f"  [수급 데이터 오류] {ticker}: {e}")
        return None

    if df is None or df.empty:
        return None

    df = df.tail(TRADING_DAYS_FOR_SUPPLY)
    result = {}

    for investor_col in ["외국인합계", "기관합계"]:
        if investor_col not in df.columns:
            continue
        series = df[investor_col]

        # 연속 순매수일수 (최근일부터 거꾸로 세기)
        consecutive = 0
        for value in reversed(series.tolist()):
            if value > 0:
                consecutive += 1
            else:
                break

        # 급증 배수 (오늘 순매수 vs 직전 10일 평균, 절대값 기준)
        if len(series) >= 11:
            today_value = series.iloc[-1]
            prior_avg = series.iloc[-11:-1].abs().mean()
            spike_ratio = (today_value / prior_avg) if prior_avg > 0 else 0
        else:
            today_value = series.iloc[-1] if len(series) > 0 else 0
            spike_ratio = 0

        result[investor_col] = {
            "연속순매수일수": consecutive,
            "오늘순매수대금": int(today_value),
            "급증배수": round(spike_ratio, 1),
        }

    return result


# ── 텔레그램 알림 ────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[텔레그램 미설정] 메시지 전송 생략")
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


# ── 메인 실행 ────────────────────────────────────────
def main():
    mentioned_stocks = get_recently_mentioned_stocks()
    print(f"최근 {LOOKBACK_DAYS_FOR_MENTIONS}일간 언급된 종목: {len(mentioned_stocks)}개")
    if not mentioned_stocks:
        print("확인할 종목이 없습니다. 종료합니다.")
        return

    ticker_map = build_or_load_ticker_map()
    seen = load_json(SEEN_FILE, {})
    unmatched = load_json(UNMATCHED_FILE, [])
    today_str = datetime.now().strftime("%Y-%m-%d")

    new_signals_summary = []

    for stock_name in mentioned_stocks:
        ticker = match_stock_name_to_ticker(stock_name, ticker_map)
        if not ticker:
            if stock_name not in unmatched:
                unmatched.append(stock_name)
            print(f"  [미매칭] {stock_name}")
            continue

        seen_key = f"{ticker}_{today_str}"
        if seen_key in seen:
            continue  # 오늘 이미 확인한 종목

        print(f"  분석 중: {stock_name} ({ticker})")
        analysis = analyze_supply_demand(ticker)
        seen[seen_key] = True
        time.sleep(0.5)  # KRX 요청 간격

        if not analysis:
            continue

        # 신호 판정
        triggered_reasons = []
        for investor_col, stats in analysis.items():
            if stats["연속순매수일수"] >= CONSECUTIVE_DAYS_THRESHOLD:
                triggered_reasons.append(
                    f"{investor_col} {stats['연속순매수일수']}일 연속 순매수"
                )
            if stats["급증배수"] >= SPIKE_MULTIPLIER_THRESHOLD:
                triggered_reasons.append(
                    f"{investor_col} 순매수 평소 대비 {stats['급증배수']}배 급증"
                )

        if triggered_reasons:
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "종목명": stock_name,
                "티커": ticker,
                "신호": triggered_reasons,
                "상세": analysis,
            }
            append_signal(record)
            new_signals_summary.append(record)

    save_json(SEEN_FILE, seen)
    save_json(UNMATCHED_FILE, unmatched)

    if new_signals_summary:
        lines = [f"💹 <b>수급 담당자 알림</b> ({len(new_signals_summary)}건)\n"]
        for r in new_signals_summary:
            lines.append(f"🔹 <b>{r['종목명']}</b> ({r['티커']})")
            for reason in r["신호"]:
                lines.append(f"   └ {reason}")
        send_telegram("\n".join(lines))
        print(f"\n총 {len(new_signals_summary)}건의 수급 신호를 찾았고, 텔레그램으로 전송했습니다.")
    else:
        print("\n특이 수급 신호가 없습니다.")

    if unmatched:
        print(f"\n[참고] 매칭 안 된 종목명 {len(unmatched)}개가 "
              f"data/supply_demand_unmatched.json 에 쌓여있습니다.")


if __name__ == "__main__":
    main()
