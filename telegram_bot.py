"""
telegram_bot.py — асинхронный отправитель сообщений в Telegram.

Транспорт построен на httpx (легче и предсказуемее aiogram для задачи
«только уведомления»): retries с экспоненциальным backoff, обработка
HTTP 429 (flood control Telegram с параметром retry_after), временных
сетевых сбоев и «мёртвых» chat_id.

Если TELEGRAM_BOT_TOKEN / CHAT_ID не заданы — нотификатор переходит в
режим DRY-RUN: сообщения печатаются в лог. Это позволяет запускать
сканер локально без Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional, Sequence

import httpx

log = logging.getLogger("telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000  # лимит Telegram API — 4096, оставляем запас
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3


class TelegramNotifier:
    """Отправка HTML-сообщений в один или несколько чатов."""

    def __init__(
        self,
        bot_token: Optional[str],
        chat_ids: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._bot_token = bot_token
        self._chat_ids = list(chat_ids)
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._client: Optional[httpx.AsyncClient] = None
        self._disabled_chats: set[str] = set()
        # DRY-RUN: без токена или без чатов отправлять некуда — пишем в лог.
        self.dry_run = not (bot_token and self._chat_ids)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.dry_run:
            log.warning(
                "Telegram DRY-RUN: TELEGRAM_BOT_TOKEN/CHAT_ID не заданы — "
                "сигналы будут печататься только в лог"
            )
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=TELEGRAM_API_BASE,
                timeout=self._timeout,
                # trust_env позволяет учитывать HTTPS_PROXY из окружения
                # (полезно на Railway при гео-блокировках).
                trust_env=True,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ public API
    async def send_html(self, html: str) -> bool:
        """Отправить HTML-сообщение во все чаты. True — доставлено везде."""
        if self.dry_run:
            preview = html.replace("\n", " | ")
            log.info("[DRY-RUN] Сообщение Telegram: %s", preview[:500])
            return True

        if self._client is None:
            await self.start()

        assert self._client is not None
        text = html if len(html) <= MAX_MESSAGE_LENGTH else html[:MAX_MESSAGE_LENGTH]

        at_least_one_sent = False
        for chat_id in self._chat_ids:
            if chat_id in self._disabled_chats:
                continue
            if await self._send_to_chat(chat_id, text):
                at_least_one_sent = True
        return at_least_one_sent

    # ------------------------------------------------------------------ internals
    async def _send_to_chat(self, chat_id: str, text: str) -> bool:
        url = f"/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.post(url, json=payload)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                log.warning(
                    "Telegram: сетевая ошибка (попытка %d/%d): %s",
                    attempt, self._max_retries, exc,
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 200:
                return True

            body = self._safe_body(response)

            # Flood control: Telegram просит подождать retry_after секунд.
            if response.status_code == 429:
                retry_after = 5.0
                try:
                    retry_after = float(response.json().get("parameters", {}).get("retry_after", 5))
                except Exception:  # noqa: BLE001 — JSON может быть невалиден
                    pass
                log.warning("Telegram: flood control, жду %.0fс", retry_after)
                await asyncio.sleep(retry_after + 1.0)
                continue

            # Некорректный chat_id / бот не добавлен в чат — не ретраим.
            if response.status_code == 400:
                log.error(
                    "Telegram: чат %s отклонил сообщение (%s) — отключаю его",
                    chat_id, body,
                )
                self._disabled_chats.add(chat_id)
                return False

            # 401/403 — невалидный токен; 5xx — проблемы на стороне Telegram.
            if response.status_code in (401, 403):
                log.error("Telegram: токен отклонён (%d): %s", response.status_code, body)
                return False

            log.warning(
                "Telegram: HTTP %d (попытка %d/%d): %s",
                response.status_code, attempt, self._max_retries, body,
            )
            await self._sleep_backoff(attempt)

        log.error("Telegram: не удалось доставить сообщение в чат %s", chat_id)
        return False

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(1.5 * (2 ** (attempt - 1)), 15.0)
        await asyncio.sleep(delay + random.uniform(0, 0.5))

    @staticmethod
    def _safe_body(response: httpx.Response) -> str:
        try:
            return response.json().get("description", response.text)[:200]
        except Exception:  # noqa: BLE001
            return response.text[:200]
