from __future__ import annotations

import asyncio
from typing import Any

from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.types import Message

from cryptopilot.ai_agent import AIAssistant


_installed = False
_auto_review_assistant: AIAssistant | None = None
_original_send_message = None

_ALERT_MARKERS = (
    "Автоматически найден сильный сетап",
    "Автоматически подтверждён сильный RID-сетап",
    "PRIME",
    "PRE-MOVE",
    "Ранний радар",
    "ПОГЛОЩЕНИЕ",
    "РАННЕЕ ДАВЛЕНИЕ",
)
_DECISION_MARKERS = ("LONG", "SHORT", "Вход", "Entry", "Стоп", "TP", "Take Profit")


def install_ai_chat() -> None:
    """Attach AI chat and advisory review without changing scanner/risk logic."""
    global _installed, _auto_review_assistant, _original_send_message
    if _installed:
        return

    import cryptopilot.telegram as telegram
    original_build_router = telegram.build_router

    def wrapped_build_router(*args: Any, **kwargs: Any):
        global _auto_review_assistant
        router = original_build_router(*args, **kwargs)
        settings = args[3] if len(args) >= 4 else kwargs["settings"]
        smart_money = args[5] if len(args) >= 6 else kwargs.get("smart_money")
        if smart_money is None:
            return router

        assistant = AIAssistant(settings, smart_money)
        _auto_review_assistant = assistant

        @router.message(Command("ai"))
        @router.message(Command("ask"))
        async def ai_command(message: Message) -> None:
            text = (message.text or "").split(maxsplit=1)
            question = text[1].strip() if len(text) == 2 else ""
            if not question:
                await message.answer(
                    "🤖 <b>AI-ассистент</b>\n\n"
                    "Напиши вопрос после /ai или просто отправь обычное сообщение.\n"
                    "Например: <code>/ai что сейчас происходит с BTC?</code>\n\n"
                    + assistant.status_text()
                )
                return
            await _answer(message, assistant, question)

        @router.message(F.text.func(lambda value: isinstance(value, str) and not value.startswith("/")))
        async def ai_free_chat(message: Message) -> None:
            text = (message.text or "").strip()
            if not text:
                return
            await _answer(message, assistant, text)

        _installed = True
        return router

    telegram.build_router = wrapped_build_router

    _original_send_message = Bot.send_message

    async def reviewed_send_message(self: Bot, chat_id: Any, text: str, *args: Any, **kwargs: Any):
        result = await _original_send_message(self, chat_id, text, *args, **kwargs)
        assistant = _auto_review_assistant
        if assistant is None or not assistant.enabled or not _looks_like_alert(text):
            return result

        async def review() -> None:
            try:
                review_text = await asyncio.wait_for(
                    assistant.answer(
                        int(chat_id),
                        "Проверь автоматически присланный ботом сигнал. "
                        "Дай независимый вывод ENTER/WAIT/AVOID, уверенность, 2-4 причины, "
                        "риск/инвалидацию и что должно подтвердиться. Не выдумывай данные.\n\n"
                        "СИГНАЛ:\n" + text,
                    ),
                    timeout=15,
                )
                if review_text:
                    await _original_send_message(
                        self,
                        chat_id,
                        "🤖 <b>AI-проверка автоматического сигнала</b>\n\n" + review_text,
                        parse_mode=None,
                    )
            except Exception:
                return

        asyncio.create_task(review())
        return result

    Bot.send_message = reviewed_send_message
    _installed = True


def _looks_like_alert(text: str) -> bool:
    if not isinstance(text, str) or len(text) < 30:
        return False
    return any(marker in text for marker in _ALERT_MARKERS) and any(
        marker in text for marker in _DECISION_MARKERS
    )


async def _answer(message: Message, assistant: AIAssistant, question: str) -> None:
    if len(question) > 6000:
        question = question[:6000]
    progress = await message.answer("🤖 Анализирую…")
    try:
        answer = await assistant.answer(message.chat.id, question)
        await progress.edit_text(answer, parse_mode=None)
    except Exception as exc:
        await progress.edit_text(f"⚠️ AI-ответ не получен: <code>{type(exc).__name__}</code>")
