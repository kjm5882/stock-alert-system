# ============================================================
# 상태 저장
# GitHub Actions는 매번 새 컴퓨터에서 실행되므로,
# 이 상태를 data/last_reports.json 파일로 저장소에 커밋해서 유지한다.
#
# 저장 구조:
# {
#   "processed": {"005930": ["rcept_no1", ...]},        # 이미 알림 보낸 공시
#   "cumulative": {"005930": {"2026_11012": 12345}},     # 정기공시의 "누적" 순이익 원본
#                                                          # (반기/3분기 실적에서 직전 분기를 빼서
#                                                          #  단일 분기 실적을 구하기 위한 재료)
#   "quarters": {"005930": {"2026Q1": {"net_income_won": 111, "source": "periodic"},
#                            "2026Q2": {"net_income_won": 222, "source": "prelim"}}}
#                                                          # 분기별 "단일 분기" 순이익.
#                                                          # 여기 최근 4개를 모으면 TTM 계산 가능.
# }
# ============================================================
import json
import os

STATE_FILE = "data/last_reports.json"


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"processed": {}, "cumulative": {}, "quarters": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("processed", {})
    data.setdefault("cumulative", {})
    data.setdefault("quarters", {})
    return data


def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- 처리한 공시 번호 ---

def get_processed(state: dict, ticker: str) -> list:
    return state.get("processed", {}).get(ticker, [])


def set_processed(state: dict, ticker: str, rcept_no_list: list):
    state.setdefault("processed", {})[ticker] = rcept_no_list


# --- 정기공시 누적 순이익 (분기 단일값 역산용 재료) ---

def get_cumulative(state: dict, ticker: str, key: str):
    return state.get("cumulative", {}).get(ticker, {}).get(key)


def set_cumulative(state: dict, ticker: str, key: str, value: float):
    state.setdefault("cumulative", {}).setdefault(ticker, {})[key] = value


# --- 분기별 단일 실적 (TTM 계산용) ---

def set_quarter(state: dict, ticker: str, quarter_key: str, net_income_won: float, source: str):
    state.setdefault("quarters", {}).setdefault(ticker, {})[quarter_key] = {
        "net_income_won": net_income_won,
        "source": source,
    }


def get_last_n_quarters(state: dict, ticker: str, n: int = 4):
    """
    가장 최근 n개 분기의 (분기키, net_income_won)을 시간순으로 반환한다.
    데이터가 n개 미만이면 있는 만큼만 반환한다 - 호출부에서 개수를 확인해서 처리해야 한다.
    """
    quarters = state.get("quarters", {}).get(ticker, {})

    def sort_key(k):
        year, q = k.split("Q")
        return (int(year), int(q))

    ordered_keys = sorted(quarters.keys(), key=sort_key)
    latest_keys = ordered_keys[-n:]
    return [(k, quarters[k]["net_income_won"]) for k in latest_keys]
