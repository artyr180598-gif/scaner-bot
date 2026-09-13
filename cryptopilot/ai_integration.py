from __future__ import annotations

from typing import Any

from aiogram import F
from aiogram.filters import Command
from aiogram.types import Message

from cryptopilot.ai_agent import AIAssistant


_installed = False


def install_ai_chat() -> None:
    """Attach AI chat without editing the existing scanner/router implementation."""
    global _installed
    if _installed:
        return

    import cryptopilot.telegram as telegram

    original_build_router = telegram.build_router

    def wrapped_build_router(*args: Any, **kwargs: Any):
        router = original_build_router(*args, **kwargs)
        settings = args[3] if len(args) >= 4 else kwargs["settings"]
        smart_money = args[5] if len(args) >= 6 else kwargs.get("smart_money")
        if smart_money is None:
            return router

        assistant = AIAssistant(settings, smart_money)

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
    _installed = True


async def _answer(message: Message, assistant: AIAssistant, question: str) -> None:
    if len(question) > 6000:
        question = question[:6000]
    progress = await message.answer("🤖 Анализирую…")
    try:
        answer = await assistant.answer(message.chat.id, question)
        # Telegram HTML parse mode is enabled globally; AI output is not trusted HTML.
        # Send as plain text by disabling parse mode for this message.
        await progress.edit_text(answer, parse_mode=None)
    except Exception as exc:
        await progress.edit_text(f"⚠️ AI-ответ не получен: <code>{type(exc).__name__}</code>")
