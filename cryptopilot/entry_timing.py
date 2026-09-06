"""Fixed conservative timing vetoes; not a probability model."""


def timing_vetoes(
    long: bool,
    execution_dmi: float,
    execution_trend: int,
    primary_trend: int,
    execution_rsi: float,
    primary_rsi: float,
    structural_rsi: float,
    execution_rvol: float,
) -> list[str]:
    sign = 1 if long else -1
    reasons = []
    if sign * execution_trend < 0 and sign * execution_dmi < -5:
        reasons.append("Младший Trend Guard и DMI против входа: ждать восстановления структуры")
    if sign * primary_trend < 0:
        reasons.append("Trend Guard основного таймфрейма против входа")
    directional_rsi = (
        (execution_rsi, primary_rsi, structural_rsi)
        if long
        else (100 - execution_rsi, 100 - primary_rsi, 100 - structural_rsi)
    )
    if directional_rsi[1] >= 78 and directional_rsi[2] >= 75:
        reasons.append("Два старших таймфрейма перегреты: поздний вход временно заблокирован")
    if directional_rsi[0] >= 75 and execution_rvol >= 3:
        reasons.append("Экстремальный объём и RSI на младшем таймфрейме: не догонять импульс")
    return reasons
