"""
수급 담당자 (Supply-Demand Agent) v3 - 네이버 API 기반
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
  3. 각 티커의 최근 수급 데이터 조회 (하루 한 번)
  4. 패턴 감지: 연속 순매수 3일 이상 / 평소 대비 3배 이상 급증
  5. 저장 + 텔레그램 알림

v3에서 고친 것 (2026-09-30 실행 로그에서 드러난 문제들)
  ① '오늘'의 기준을 UTC → KST 로 바꿈
     UTC 자정은 한국시간 오전 9시다. 그래서 평일 19시 정기 실행과
     다음날 아침 8시 수동 실행이 UTC로는 같은 날이 되어,
     아침 실행이 전 종목을 건너뛰고 '조회 성공 0개'로 끝났다.
  ② seen 파일이 무한히 커지던 것을 최근 며칠치만 남기도록 정리
     종목×날짜마다 키가 하나씩 쌓이는데 지우는 코드가 없었다.
     95종목이면 하루 95개, 1년이면 2만 개가 넘는다.
  ③ 조회에 실패해도 '봤음'으로 표시하던 것을 성공했을 때만 표시하도록 수정
     네이버가 한 번 실패하면 그날은 재시도가 없었다.
     피드 담당자에서 똑같은 문제를 겪고 고쳤던 부분이다.
  ④ 별칭 목록을 매칭 엔진과 맞춤
     여기엔 5개뿐이라 '한조' '현대중공업' '마컨솔'이 전부 미매칭으로 빠졌다.
     두 담당자가 서로 다른 종목을 보고 있었다.
  ⑤ 미매칭 목록을 매번 새로 만들도록 수정
     예전에는 한 번 들어간 이름이 영원히 남아서, 별칭을 추가해 해결된
     종목도 계속 미매칭으로 집계됐다 (28개 출력 / 79개 집계).
  ⑥ 괄호 표기를 정규화
     '제이앤티씨 (JNTC)' 처럼 블로그가 영문명을 덧붙인 경우를 잡아낸다.

주의: 네이버 API 구조가 바뀌면 조용히 실패할 수 있으므로,
     데이터를 전혀 못 가져오면 텔레그램으로 경고를 보냅니다.
"""

import os
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests

# ── 설정 ──────────────────────────────────────────────
LOOKBACK_DAYS_FOR_MENTIONS = 14     # 피드에서 며칠치 언급을 볼지
CONSECUTIVE_DAYS_THRESHOLD = 3      # 연속 순매수 며칠부터 신호로 볼지
SPIKE_MULTIPLIER_THRESHOLD = 3.0    # 평소 대비 몇 배부터 급증으로 볼지
SEEN_KEEP_DAYS = 7                  # seen 기록을 며칠치만 남길지

KST = timezone(timedelta(hours=9))  # GitHub Actions는 UTC로 도니 명시적으로 변환한다

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
# 매칭 엔진(matching_engine.py)의 ALIAS_MAP과 같은 내용을 유지해야 한다.
# 한쪽에만 있으면 두 담당자가 서로 다른 종목을 분석하게 된다.
ALIAS_MAP = {
    "콜마": "한국콜마",
    "코스메카": "코스메카코리아",
    "SKT": "SK텔레콤",
    "네이버": "NAVER",
    "한조": "HD한국조선해양",
    "한국조선해양": "HD한국조선해양",
    "현대중공업": "HD현대중공업",
    "현대일렉트릭": "HD현대일렉트릭",
    "하이닉스": "SK하이닉스",
    "마컨솔": "마이크로컨텍솔",
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


def prune_seen(seen, today):
    """seen 기록에서 최근 SEEN_KEEP_DAYS 일치만 남긴다.

    키는 '{티커}_{YYYY-MM-DD}' 형태다. 날짜를 못 읽는 키는 오래된 것으로 보고 버린다.
    """
    cutoff = today - timedelta(days=SEEN_KEEP_DAYS)
    kept = {}
    for key, value in seen.items():
        m = re.search(r"(\d{4}-\d{2}-\d{2})$", key)
        if not m:
            continue
        try:
            day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            kept[key] = value
    return kept


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
    """저장된 매핑 사전에서 종목명을 티커로 변환.

    블로그 글에는 '제이앤티씨 (JNTC)' 처럼 영문명이나 설명이 괄호로 붙는 경우가 많다.
    정식 명칭으로 먼저 찾고, 안 되면 표기를 차례로 벗겨내며 다시 찾는다.
    """
    candidates = [stock_name]

    if stock_name in ALIAS_MAP:
        candidates.append(ALIAS_MAP[stock_name])

    # 괄호와 그 안의 내용 제거: '제이앤티씨 (JNTC)' → '제이앤티씨'
    no_paren = re.sub(r"[(（][^)）]*[)）]", "", stock_name).strip()
    if no_paren and no_paren != stock_name:
        candidates.append(no_paren)
        if no_paren in ALIAS_MAP:
            candidates.append(ALIAS_MAP[no_paren])

    # 법인 표기 제거
    for base in list(candidates):
        stripped = (base.replace("(주)", "").replace("㈜", "")
                        .replace("주식회사", "").strip())
        if stripped and stripped != base:
            candidates.append(stripped)

    for cand in candidates:
        if cand in ticker_map:
            return ticker_map[cand]
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

    today = datetime.now(KST).date()          # ← UTC가 아니라 KST 기준의 '오늘'
    today_str = today.isoformat()

    seen_before = load_json(SEEN_FILE, {})
    seen = prune_seen(seen_before, today)
    if len(seen_before) != len(seen):
        print(f"오래된 조회 기록 {len(seen_before) - len(seen)}건 정리 "
              f"(최근 {SEEN_KEEP_DAYS}일치만 유지)")

    # 미매칭 목록은 매번 새로 만든다. 예전에는 누적만 되어서
    # 별칭을 추가해 해결된 종목까지 계속 미매칭으로 집계됐다.
    unmatched = []

    new_signals = []
    fetch_success = 0
    fetch_attempt = 0
    skipped_today = 0

    for name in sorted(mentioned):
        ticker = match_stock_name_to_ticker(name, ticker_map)
        if not ticker:
            unmatched.append(name)
            print(f"  [미매칭] {name}")
            continue

        seen_key = f"{ticker}_{today_str}"
        if seen_key in seen:
            skipped_today += 1
            continue

        print(f"  분석 중: {name} ({ticker})")
        fetch_attempt += 1
        trend = fetch_trend(ticker)
        time.sleep(0.4)

        if not trend:
            # 실패한 종목은 '봤음'으로 표시하지 않는다.
            # 표시해버리면 네이버가 한 번 흔들린 날은 재시도 기회가 사라진다.
            continue

        seen[seen_key] = True
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

    # 왜 조회를 안 했는지 로그에 남긴다.
    # 예전에는 '조회 성공 0개'만 찍혀서, 오늘 이미 받아온 것인지
    # 네이버가 죽은 것인지 로그만 보고는 구분할 수 없었다.
    if skipped_today:
        print(f"[참고] {skipped_today}개 종목은 오늘({today_str}, KST) "
              f"이미 조회해서 건너뛰었습니다.")

    if unmatched:
        print(f"[참고] 미매칭 종목명 {len(unmatched)}개 "
              f"(data/supply_demand_unmatched.json 확인)")


if __name__ == "__main__":
    main()
