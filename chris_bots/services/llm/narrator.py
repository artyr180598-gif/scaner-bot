"""
Генератор нарратива «Логика входа».

Два режима:
- LLM (если LLM_ENABLED=true и есть API-ключ): короткий prompt в OpenAI/Anthropic.
- Шаблон (fallback): детерминированная фраза от «Крис» на основе факторов.

Любой сценарий выдаёт связный текст на русском, в стиле «Крис объясняет простым
языком, без перегруза терминами».
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from ...config.settings import Settings
from ...core.domain.signal import Direction, Signal

log = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Ты — Крис, аналитическая модель экосистемы Chris Bets. "
    "Объясняй просто, как старший коллега новичку. "
    "Не используй сложные термины без расшифровки. "
    "Говори от первого лица: «я вижу», «мой вывод», «жду». "
    "Без воды, 2-4 предложения. Без обещаний прибыли."
)


@dataclass(slots=True)
class Narrative:
    text: str
    factors: List[str]
    source: str  # "llm" | "template"


class Narrator:
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

    async def narrate(self, signal: Signal) -> Narrative:
        """
        Возвращает Narrative. Если LLM включена и доступна — пробует её;
        при любой ошибке — fallback на шаблон.
        """
        factors = self._select_factors(signal)
        if self.s.llm_enabled and self.s.llm_api_key:
            try:
                text = await self._call_llm(signal, factors)
                return Narrative(text=text, factors=factors, source="llm")
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM call failed, fallback to template: %s", exc)
        text = self._template(signal, factors)
        return Narrative(text=text, factors=factors, source="template")

    # ── LLM ───────────────────────────────────────────────────
    async def _call_llm(self, signal: Signal, factors: List[str]) -> str:
        user = (
            f"Актив: {signal.symbol}. Направление: {signal.direction.value}. "
            f"Цена: {signal.last_price}. Таймфреймы: {', '.join(signal.timeframes_used)}. "
            f"Уверенность data/signal: {signal.confidences.data:.0f}/{signal.confidences.signal:.0f}. "
            f"Ключевые факторы: {', '.join(factors) or 'нет явных'}. "
            f"Цель 1: {signal.plan.take_profits[0].price if signal.plan.take_profits else 'n/a'}. "
            f"Стоп: {signal.plan.stop_loss.price if signal.plan.stop_loss else 'n/a'}. "
            "Объясни логику входа в 2-4 предложениях от лица Крис."
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
        """Выбираем 2-3 ключевых фактора из групп."""
        out: List[str] = []
        caps = signal.confidences.caps or {}
        # Сортируем группы по |score|, берём топ-3.
        ranked = sorted(caps.items(), key=lambda kv: -abs(kv[1]))
        for grp, score in ranked[:3]:
            if abs(score) < 0.1:
                continue
            human = {
                "trend": "тренд",
                "momentum": "моментум (RSI/MACD)",
                "volume": "объём",
                "volatility": "волатильность",
                "structure": "структура рынка",
                "patterns": "свечной паттерн",
            }.get(grp, grp)
            side = "бычий" if score > 0 else "медвежий"
            out.append(f"{human} ({side})")
        return out

    def _template(self, signal: Signal, factors: List[str]) -> str:
        side_word = "покупку" if signal.direction == Direction.LONG else "продажу"
        if not factors:
            return (
                f"Я смотрю на {signal.symbol}: техническая картина нейтральная, "
                f"явного сетапа не вижу — пропускаю."
            )
        if signal.direction == Direction.LONG:
            lead = "жду продолжения роста"
            verb = "подтверждают"
        else:
            lead = "жду продолжения снижения"
            verb = "подтверждают"
        factors_str = ", ".join(factors[:-1]) + (" и " + factors[-1] if len(factors) > 1 else factors[0])
        return (
            f"Смотрю на {signal.symbol}: {lead}. "
            f"Сигналы по группе «{factors_str}» {verb} направление. "
            f"Вход — в зоне {signal.plan.entry_zone[0]:.4f}–{signal.plan.entry_zone[1]:.4f}, "
            f"первая цель — {signal.plan.take_profits[0].price:.4f}."
        )
