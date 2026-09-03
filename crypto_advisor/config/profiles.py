"""
Профили риска для подбора монет (как «паттерны поведения» в OctoBot).

Профиль — это набор критериев, который превращается в UserRequest.
Их использует и Telegram-кнопка быстрого выбора, и парсер текста.
"""
from __future__ import annotations

from dataclasses import dataclass

RISK_PROFILES = ("conservative", "balanced", "aggressive")


@dataclass(frozen=True)
class RiskProfile:
    id: str
    label: str
    emoji: str
    description: str
    # Критерии по умолчанию
    min_volume_usd_24h: float
    min_confidence: float
    min_atr_pct: float
    max_atr_pct: float
    # Насколько агрессивно брать волатильные/импульсные активы (0..1)
    appetite: float


PROFILES: dict[str, RiskProfile] = {
    "conservative": RiskProfile(
        id="conservative",
        label="Консервативный",
        emoji="🛡️",
        description="Низкая волатильность, ликвидность важнее всего, "
                    "импульс только с подтверждением тренда.",
        min_volume_usd_24h=10_000_000.0,
        min_confidence=72.0,
        min_atr_pct=0.4,
        max_atr_pct=5.0,
        appetite=0.15,
    ),
    "balanced": RiskProfile(
        id="balanced",
        label="Сбалансированный",
        emoji="⚖️",
        description="Золотая середина: умеренный импульс + согласованные "
                    "группы факторов.",
        min_volume_usd_24h=5_000_000.0,
        min_confidence=63.0,
        min_atr_pct=0.3,
        max_atr_pct=9.0,
        appetite=0.5,
    ),
    "aggressive": RiskProfile(
        id="aggressive",
        label="Агрессивный",
        emoji="🚀",
        description="Высокая волатильность, ловим импульс раньше всех, "
                    "допускаем большие движения.",
        min_volume_usd_24h=3_000_000.0,
        min_confidence=55.0,
        min_atr_pct=0.6,
        max_atr_pct=14.0,
        appetite=0.85,
    ),
}


def get_profile(profile_id: str) -> RiskProfile:
    return PROFILES.get(profile_id, PROFILES["balanced"])


def human_label(profile_id: str) -> str:
    return PROFILES.get(profile_id, PROFILES["balanced"]).label
