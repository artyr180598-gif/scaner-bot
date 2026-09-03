"""Скоринг: data-confidence + сборка Confidences для сигнала."""

from __future__ import annotations

from typing import Dict

import pandas as pd

from ..core.domain.signal import Confidences, Direction


def score_data(df: pd.DataFrame) -> float:
    """Data confidence: полнота истории и отсутствие NaN (0..100)."""
    if len(df) < 10:
        return 0.0
    length_score = min(1.0, len(df) / 100.0) * 100
    non_nan = df.notna().mean().mean() * 100
    return round(min(length_score, non_nan), 1)


def build_confidences(
    df: pd.DataFrame,
    group_scores: Dict[str, float],
    direction: Direction,
    signal_confidence: float,
    risk_profile: str = "balanced",
) -> Confidences:
    return Confidences(
        data=score_data(df),
        signal=signal_confidence,
        risk_profile=risk_profile,
        group_caps={g: round(float(s), 3) for g, s in group_scores.items()},
    )
