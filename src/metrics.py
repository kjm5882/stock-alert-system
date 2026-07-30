# ============================================================
# 지표 계산
# - 영업이익률, 순이익률, 부채비율, ROE 등
# - 전분기/전년동기 대비 성장률은 추후 데이터가 누적되면 자동 계산되도록 확장 가능
# ============================================================

def calc_ratios(accounts: dict) -> dict:
    """DART 핵심 계정으로 기본 재무비율을 계산한다."""
    revenue = accounts.get("매출액")
    op_income = accounts.get("영업이익")
    net_income = accounts.get("당기순이익")
    assets = accounts.get("자산총계")
    liabilities = accounts.get("부채총계")
    equity = accounts.get("자본총계")

    ratios = {}

    if revenue and op_income is not None:
        ratios["영업이익률(%)"] = round(op_income / revenue * 100, 2)
    if revenue and net_income is not None:
        ratios["순이익률(%)"] = round(net_income / revenue * 100, 2)
    if equity and liabilities is not None:
        ratios["부채비율(%)"] = round(liabilities / equity * 100, 2) if equity else None
    if equity and net_income is not None and equity != 0:
        ratios["ROE(%)"] = round(net_income / equity * 100, 2)

    return ratios


def format_report(name: str, accounts: dict, ratios: dict, valuation: dict, price: dict) -> str:
    """Telegram 메시지용 텍스트로 포맷팅한다."""
    lines = [f"📊 {name}"]

    if accounts:
        lines.append("— 실적 —")
        for k, v in accounts.items():
            if v is not None:
                lines.append(f"  {k}: {v:,}원")

    if ratios:
        lines.append("— 재무비율 —")
        for k, v in ratios.items():
            if v is not None:
                lines.append(f"  {k}: {v}")

    if valuation:
        lines.append("— 밸류에이션 —")
        for k, v in valuation.items():
            if v is not None:
                lines.append(f"  {k}: {v}")

    if price:
        lines.append("— 시세 —")
        for k, v in price.items():
            if v is not None:
                lines.append(f"  {k}: {v:,}" if isinstance(v, (int, float)) else f"  {k}: {v}")

    return "\n".join(lines)
