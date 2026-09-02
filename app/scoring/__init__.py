"""app/scoring — свёртка факторов в оценку, направление и уверенность."""

from app.scoring.scorer import (  # noqa: F401
    GroupScore,
    ScoreResult,
    collapse_groups,
    confidence_label,
    potential_label,
    score_factors,
)
from app.scoring.weights import GROUP_CONFIDENCE_CAPS, GROUP_WEIGHTS  # noqa: F401
