"""Descriptive paper accounting, never a profitability certification."""

import math
from collections import Counter


def statistics(records, version):
    current = [r for r in records if r.get("version") == version]
    counts = Counter(r.get("status", "UNKNOWN") for r in current)
    closed = [r for r in current if r.get("status") == "CLOSED"]
    valid = [
        r
        for r in closed
        if all(
            isinstance(r.get(k), (int, float)) and math.isfinite(r[k])
            for k in ("net_r", "stress_r", "closed_ms")
        )
    ]
    valid.sort(key=lambda r: r["closed_ms"])
    equity = peak = drawdown = 0.0
    for row in valid:
        equity += row["net_r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    censored = sum(n for status, n in counts.items() if status.startswith("CENSORED"))
    return dict(
        total=len(current),
        other_versions=len(records) - len(current),
        open=counts["OPEN"],
        closed=len(valid),
        censored=censored,
        invalid_closed=len(closed) - len(valid),
        wins=sum(r["net_r"] > 0 for r in valid),
        net_r=sum(r["net_r"] for r in valid),
        stress_r=sum(r["stress_r"] for r in valid),
        drawdown_r=drawdown,
        censor_reasons={s: n for s, n in sorted(counts.items()) if s.startswith("CENSORED")},
    )


def format_statistics(records, version):
    s = statistics(records, version)
    text = (
        f"Версия эксперимента: {version}\n"
        f"Записей: {s['total']} · открыто: {s['open']} · закрыто: {s['closed']}\n"
        f"Неоднозначные данные: {s['censored']} · повреждённые итоги: {s['invalid_closed']}\n"
    )
    if s["other_versions"]:
        text += f"Другие/неизвестные версии: {s['other_versions']} — не смешиваются.\n"
    if s["closed"]:
        text += (
            f"Только оценённые закрытые сделки: прибыльных {s['wins']}/{s['closed']}\n"
            f"Сумма: {s['net_r']:+.2f}R · среднее: {s['net_r'] / s['closed']:+.3f}R\n"
            f"При удвоенных издержках: {s['stress_r']:+.2f}R суммарно\n"
            f"Просадка суммы закрытых результатов: {s['drawdown_r']:.2f}R\n"
        )
    else:
        text += "Оценённых закрытых сделок пока нет.\n"
    labels = {
        "CENSORED_GAP": "пропуск минутных данных",
        "CENSORED_ENTRY_MINUTE": "неоднозначная минута входа",
        "CENSORED_TIME_BOUNDARY": "нет точной цены на границе 72 часов",
    }
    for reason, n in s["censor_reasons"].items():
        text += f"• {labels.get(reason, reason)}: {n}\n"
    return text + (
        "R — первоначальный риск виртуальной сделки, не процент депозита.\n"
        "Открытые позиции и пропуски не входят в итог: возможна систематическая ошибка. "
        "Просадка не учитывает плавающие убытки и общий капитал. "
        "Доля прибыльных сделок — не вероятность успеха нового сигнала."
    )
