import sys
import os
import time
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dart_client, krx_client, metrics, telegram_notify
from pykrx import stock


def current_report_period():
    """현재 월 기준으로 가장 최근에 나왔을 법한 분기보고서 (연도, reprt_code)를 추정한다."""
    month = datetime.now().month
    year = datetime.now().year
    if month in (1, 2, 3, 4):
        return str(year - 1), "11011"   # 사업보고서(전년도 연간)
    elif month in (5, 6, 7):
        return str(year), "11013"       # 1분기보고서
    elif month in (8, 9, 10):
        return str(year), "11012"       # 반기보고서
    else:
        return str(year), "11014"       # 3분기보고서


def screen(op_margin_min: float, per_max: float):
    trading_day = krx_client.get_latest_trading_day()

    # 1단계: 전체 종목 PER 한번에 조회 (KOSPI + KOSDAQ)
    fundamentals = []
    for market in ("KOSPI", "KOSDAQ"):
        df = stock.get_market_fundamental(trading_day, market=market)
        df = df.reset_index().rename(columns={"티커": "ticker"})
        df["market"] = market
        fundamentals.append(df)

    import pandas as pd
    all_df = pd.concat(fundamentals, ignore_index=True)

    # PER이 0 이하(적자 등으로 의미 없는 값)이거나 조건 미달인 종목 제외
    candidates = all_df[(all_df["PER"] > 0) & (all_df["PER"] <= per_max)]

    print(f"1단계 통과 (PER <= {per_max}): {len(candidates)}개 종목")

    year, report_code = current_report_period()
    corp_map = {c["ticker"]: c for c in dart_client.get_all_listed_corps()}

    final_results = []

    for _, row in candidates.iterrows():
        ticker = row["ticker"]
        per = row["PER"]

        corp_info = corp_map.get(ticker)
        if not corp_info:
            continue

        try:
            statement = dart_client.get_financial_statement(corp_info["corp_code"], year, report_code)
            if not statement:
                statement = dart_client.get_financial_statement(
                    corp_info["corp_code"], year, report_code, fs_div="OFS"
                )
            if not statement:
                continue

            accounts = dart_client.extract_key_accounts(statement)
            ratios = metrics.calc_ratios(accounts)
            op_margin = ratios.get("영업이익률(%)")

            if op_margin is not None and op_margin >= op_margin_min:
                final_results.append({
                    "name": corp_info["name"],
                    "ticker": ticker,
                    "PER": round(per, 2),
                    "영업이익률(%)": op_margin,
                })

        except Exception:
            continue  # 개별 종목 오류는 건너뛰고 계속 진행 (전체 스크리닝이 멈추지 않도록)

        time.sleep(0.05)  # DART API 과다호출 방지용 짧은 대기

    return final_results


def format_results(results: list, op_margin_min: float, per_max: float) -> str:
    if not results:
        return f"🔍 스크리닝 결과 없음\n조건: 영업이익률 ≥{op_margin_min}% / PER ≤{per_max}\n조건을 만족하는 종목이 없습니다."

    results_sorted = sorted(results, key=lambda x: x["영업이익률(%)"], reverse=True)

    lines = [f"🔍 스크리닝 결과 ({len(results)}개)", f"조건: 영업이익률 ≥{op_margin_min}% / PER ≤{per_max}\n"]
    for r in results_sorted:
        lines.append(f"• {r['name']}({r['ticker']}) | 영업이익률 {r['영업이익률(%)']}% | PER {r['PER']}")

    return "\n".join(lines)


def run():
    op_margin_min = float(os.environ.get("OP_MARGIN_MIN", "20"))
    per_max = float(os.environ.get("PER_MAX", "10"))

    results = screen(op_margin_min, per_max)
    message = format_results(results, op_margin_min, per_max)

    # 텔레그램 메시지 길이 제한(4096자) 고려해서 필요시 나눠 전송
    if len(message) > 3800:
        chunks = [message[i:i+3800] for i in range(0, len(message), 3800)]
        for chunk in chunks:
            telegram_notify.send_message(chunk)
    else:
        telegram_notify.send_message(message)

    print(message)


if __name__ == "__main__":
    run()
