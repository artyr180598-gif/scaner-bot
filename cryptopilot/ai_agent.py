from __future__ import annotations

import os
import re
from collections import deque
from typing import Any

from openai import AsyncOpenAI

from cryptopilot.smart_money import SmartMoneyScanner, format_smart_money_setup


_TICKER_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{1,11})(?:/USDT|USDT)?(?![A-Z0-9])")
_STOPWORDS = {
    "THE", "AND", "FOR", "WITH", "WHAT", "WHEN", "WHERE", "WHY", "HOW",
    "LONG", "SHORT", "WAIT", "TRADE", "PRICE", "MARKET", "BTCUSDT", "USDT",
}


class AIAssistant:
    """Optional AI layer. It never changes the deterministic trading engine."""

    def __init__(self, settings: Any, smart_money: SmartMoneyScanner) -> None:
        self.settings = settings
        self.smart_money = smart_money
        self.api_key = (
            os.getenv("OPENAI_API_KEY", "").strip()
            or os.getenv("AI_API_KEY", "").strip()
            or os.getenv("OPENAI_KEY", "").strip()
        )
        self.model = os.getenv("AI_MODEL", "gpt-5.6").strip()
        self.base_url = os.getenv("AI_BASE_URL", "").strip() or None
        self.web_search = os.getenv("AI_WEB_SEARCH", "true").strip().lower() not in {
            "0", "false", "no", "off"
        }
        self.max_history = max(2, int(os.getenv("AI_CHAT_HISTORY", "8")))
        self._history: dict[int, deque[tuple[str, str]]] = {}
        self._previous_response: dict[int, str] = {}
        self.client = (
            AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            if self.api_key
            else None
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def status_text(self) -> str:
        if not self.enabled:
            return (
                "🤖 AI-ассистент: выключен. Добавьте OPENAI_API_KEY в Railway Variables."
            )
        web = "включён" if self.web_search else "выключен"
        return f"🤖 AI-ассистент: включён · модель {self.model} · web search {web}"

    async def answer(self, chat_id: int, text: str) -> str:
        if not self.enabled:
            return (
                "🤖 AI-ассистент пока не подключён.\n\n"
                "Добавьте API-ключ как Railway Variable <code>OPENAI_API_KEY</code>. "
                "Сам ключ сюда в чат присылать не нужно."
            )

        market_context = await self._market_context(text)
        history = self._history.setdefault(chat_id, deque(maxlen=self.max_history))
        conversation = "\n".join(
            f"{role}: {message}" for role, message in history
        )
        prompt = (
            "Пользователь пишет тебе как личному AI-ассистенту внутри CryptoPilot.\n"
            "Отвечай по-русски, кратко и конкретно. Если вопрос требует свежих данных, "
            "используй web search. Не выдумывай котировки, новости или результаты тестов.\n"
            "Ты можешь объяснять рынок, разбирать сетапы, искать идеи для улучшения "
            "стратегий и кода, но не выдавай себя за гарантию прибыли.\n"
            "Существующий детерминированный движок является источником торговых данных: "
            "не предлагай обходить его risk-gates и не изменяй RID/PRIME только по одному "
            "LLM-ответу. Любое изменение стратегии должно сначала проходить backtest/paper.\n\n"
            f"Текущий контекст рынка, если найден:\n{market_context}\n\n"
            f"Предыдущий мини-чат:\n{conversation or '(нет)'}\n\n"
            f"Новый вопрос пользователя:\n{text}"
        )
        instructions = (
            "Ты — встроенный AI Analyst CryptoPilot. "
            "Разделяй факты, гипотезы и предположения. Для торговых решений предпочитай "
            "WAIT, если данных недостаточно. Никогда не советуй увеличивать плечо ради "
            "компенсации убытка; в этом проекте максимум плеча задаётся конфигурацией."
        )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": prompt,
            "max_output_tokens": 1400,
        }
        if self.web_search:
            kwargs["tools"] = [{"type": "web_search_preview"}]
        previous = self._previous_response.get(chat_id)
        if previous:
            kwargs["previous_response_id"] = previous

        try:
            response = await self.client.responses.create(**kwargs)
        except Exception as exc:
            # A bad model/tool combination must not break the Telegram bot.
            if "tools" in kwargs or "previous_response_id" in kwargs:
                kwargs.pop("tools", None)
                kwargs.pop("previous_response_id", None)
                try:
                    response = await self.client.responses.create(**kwargs)
                except Exception:
                    return f"⚠️ AI временно недоступен: <code>{type(exc).__name__}</code>"
            else:
                return f"⚠️ AI временно недоступен: <code>{type(exc).__name__}</code>"

        answer = getattr(response, "output_text", None) or self._extract_output(response)
        answer = answer.strip() or "Не получил текстовый ответ от AI."
        history.append(("user", text))
        history.append(("assistant", answer))
        response_id = getattr(response, "id", None)
        if response_id:
            self._previous_response[chat_id] = response_id
        return answer

    async def _market_context(self, text: str) -> str:
        symbol = self._extract_symbol(text)
        if not symbol:
            return "Тикер в вопросе не определён."
        try:
            setup = await self.smart_money.analyze_symbol(symbol)
            if setup is None:
                return f"По {symbol} текущий глубокий Smart Money/PRIME анализ не дал объекта."
            return format_smart_money_setup(setup)
        except Exception as exc:
            return f"Текущий анализ {symbol} недоступен: {type(exc).__name__}."

    @staticmethod
    def _extract_symbol(text: str) -> str | None:
        for raw in _TICKER_RE.findall(text.upper()):
            if raw in _STOPWORDS:
                continue
            if len(raw) >= 2:
                return raw if raw.endswith("USDT") else f"{raw}USDT"
        return None

    @staticmethod
    def _extract_output(response: Any) -> str:
        chunks: list[str] = []
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                value = getattr(content, "text", None)
                if value:
                    chunks.append(value)
        return "\n".join(chunks)
