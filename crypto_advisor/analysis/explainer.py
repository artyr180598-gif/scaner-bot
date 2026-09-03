"""
Explainer — генерирует «краткое описание, почему такой сигнал».

Два режима:
- LLM (если LLM_ENABLED=true и есть API-ключ): короткий prompt.
- Шаблон (fallback): детерминированная фраза на основе факторов.

Любой сценарий выдаёт связный текст на русском, честно отмечая,
что это согласие факторов, а не гарантия прибыли.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List

from ..config.settings import Settings
from ..core.domain.query import UserRequest
from ..core.domain.signal import Direction, Signal

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — честный крипто-советник. Объясняй просто, как старший коллега новичку. "
    "Говори от первого лица: «я вижу», «мой вывод». "
    "2-4 предложения, без воды, без обещаний прибыли. "
    "Обязательно упомяни риск и что это не финансовая рекомендация."
)

_HUMAN_GROUP = {
    "trend": "тренд",
    "momentum": "моментум (RSI/MACD)",
    "volume": "объём",
    "volatility": "волатильность",
    "structure": "структура рынка",
    "patterns": "свечной паттерн",
}


@dataclass(slots=True)
class Explanation:
    text: str
    factors: List[str]
    source: str  # "template" | "llm"


class Explainer:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._client = None

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def explain(self, signal: Signal, request: UserRequest) -> Explanation:
        factors = self._select_factors(signal)
        if self.s.llm_enabled and self.s.llm_api_key:
            try:
                text = await self._call_llm(signal, request, factors)
                return Explanation(text=text, factors=factors, source="llm")
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM call failed, fallback to template: %s", exc)
        text = self._template(signal, request, factors)
        return Explanation(text=text, factors=factors, source="template")

    # ── LLM ───────────────────────────────────────────────────
    async def _call_llm(self, signal: Signal, request: UserRequest, factors: List[str]) -> str:
        user = (
            f"Актив: {signal.symbol}. Направление: {signal.direction.value}. "
            f"Цена: {signal.last_price:.4f}. ТФ: {', '.join(signal.timeframes_used)}. "
            f"Уверенность (согласие факторов): {signal.confidences.signal:.0f}%. "
            f"Запрос пользователя: {request.summary}. "
            f"Ключевые факторы: {', '.join(factors) or 'нет явных'}. "
            f"Цель 1: {signal.plan.take_profits[0].price:.4f} если есть. "
            f"Стоп: {signal.plan.stop_loss.price:.4f} если есть. "
            "Объясни логику входа в 2-4 предложениях от первого лица."
        )
        if self.s.llm_provider == "openai":
            return await self._openai(user)
        if self.s.llm_provider == "anthropic":
            return await self._anthropic(user)
        raise RuntimeError(f"unknown LLM provider {self.s.llm_provider!r}")

    async def _openai(self, user: str) -> str:
        try:
            from openai import AsyncOpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openai package not installed") from exc
        client = self._client or AsyncOpenAI(api_key=self.s.llm_api_key)
        self._client = client
        resp = await client.chat.completions.create(
            model=self.s.llm_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=self.s.llm_max_tokens,
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()

    async def _anthropic(self, user: str) -> str:
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("anthropic package not installed") from exc
        client = self._client or AsyncAnthropic(api_key=self.s.llm_api_key)
        self._client = client
        msg = await client.messages.create(
            model=self.s.llm_model,
            max_tokens=self.s.llm_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text.strip()

    # ── Template ──────────────────────────────────────────────
    @staticmethod
    def _select_factors(signal: Signal) -> List[str]:
        caps = signal.confidences.group_caps or {}
        ranked = sorted(caps.items(), key=lambda kv: -abs(kv[1]))
        out: List[str] = []
        for grp, score in ranked[:4]:
            if abs(score) < 0.10:
                continue
            human = _HUMAN_GROUP.get(grp, grp)
            side = "бычий" if score > 0 else "медвежий"
            out.append(f"{human} ({side})")
        return out

    def _template(self, signal: Signal, request: UserRequest, factors: List[str]) -> str:
        side_word = "покупку" if signal.direction == Direction.LONG else "продажу"
        if not factors:
            return (
                f"Я смотрю на {signal.symbol}: картина нейтральная, явного сетапа не вижу. "
                "Пропускаю. Это не финансовая рекомендация."
            )
        lead = "жду продолжения роста" if signal.direction == Direction.LONG else "жду продолжения снижения"
        verdict = "подтверждают" if signal.direction == Direction.LONG else "складываются против"
        factors_str = ", ".join(factors[:-1]) + (" и " + factors[-1] if len(factors) > 1 else factors[0])
        entry = signal.plan.entry_zone
        stop = signal.plan.stop_loss
        stop_phrase = (stop.rationale if stop else "за локальным уровнем").lstrip("За ")
        return (
            f"Смотрю на {signal.symbol}: {lead}. Группы факторов — {factors_str} — "
            f"{verdict} такую {side_word}. Вход — в зоне {entry[0]:.4f}–{entry[1]:.4f}, "
            f"стоп {stop_phrase}. "
            "Это согласие технических факторов, а не гарантия прибыли."
        )
