"""Middleware контроля доступа."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from ..config.settings import Settings


class AccessControlMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        chat_id = self._chat_id(event)
        if chat_id is None:
            return await handler(event, data)

        if self.s.admin_chat_ids and chat_id not in self.s.admin_chat_ids:
            if not self.s.allowed_chat_ids:
                return await self._deny(event)
            if chat_id not in self.s.allowed_chat_ids:
                return await self._deny(event)

        return await handler(event, data)

    @staticmethod
    def _chat_id(event: Any) -> int | None:
        if isinstance(event, Message):
            return event.chat.id
        if isinstance(event, CallbackQuery):
            return event.message.chat.id if event.message else None
        return None

    @staticmethod
    async def _deny(event: Any) -> None:
        text = "⛔ Доступ запрещён."
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
