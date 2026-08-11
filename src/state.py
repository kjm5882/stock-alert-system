# ============================================================
# 상태 저장
# GitHub Actions는 매번 새 컴퓨터에서 실행되므로,
# 이 상태를 data/last_reports.json 파일로 저장소에 커밋해서 유지한다.
#
# 저장 구조:
# {
#   "processed": {"005930": ["rcept_no1", ...]},   # 이미 알림 보낸 공시
#   "quarters":  {"005930": {"2026Q1": {"net_income_won": 111, "source": "periodic"},
#                             "2026Q2": {"net_income_won": 222, "source": "prelim"}}}
#                                                    # 분기별 "단일 분기" 순이익.
#                                                    # 최근 4개를 모으면 TTM 계산 가능.
# }
# ============================================================
import json
import os

STATE_FILE = "data/last_reports.json"


def load_state(path: str = STATE_FILE) -> dict:
    if not os.path.exists(path):
        return {"processed": {}, "quarters": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("processed", {})
    data.setdefault("quarters", {})
    return data


def save_state(state: dict, path: str = STATE_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- 처리한 공시 번호 ---

def get_processed(state: dict, ticker: str) -> list:
    return state.get("processed", {}).get(ticker, [])


def set_processed(state: dict, ticker: str, rcept_no_list: list):
    state.setdefault("processed", {})[ticker] = rcept_no_list


# --- 분기별 단일 실적 (TTM 계산용) ---

def set_quarter(state: dict, ticker: str, quarter_key: str, net_income_won: float, source: str):
    state.setdefault("quarters", {}).setdefault(ticker, {})[quarter_key] = {
        "net_income_won": net_income_won,
        "source": source,
    }


def get_quarters_for_ticker(state: dict, ticker: str) -> dict:
    """해당 종목의 저장된 모든 분기 데이터를 {분기키: net_income_won} 형태로 반환."""
    quarters = state.get("quarters", {}).get(ticker, {})
    return {k: v["net_income_won"] for k, v in quarters.items()}


def get_last_n_quarters(state: dict, ticker: str, n: int = 4):
    """가장 최근 n개 분기의 (분기키, net_income_won)을 시간순으로 반환한다."""
    quarters = get_quarters_for_ticker(state, ticker)

    def sort_key(k):
        year, q = k.split("Q")
        return (int(year), int(q))

    ordered_keys = sorted(quarters.keys(), key=sort_key)
    latest_keys = ordered_keys[-n:]
    return [(k, quarters[k]) for k in latest_keys]
