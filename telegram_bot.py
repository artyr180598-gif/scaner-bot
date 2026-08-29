"""
telegram_bot.py — асинхронный Telegram-транспорт бота.

Два направления работы:

1. ИСХОДЯЩИЕ (автономные) уведомления — ArbitrageScanner сам присылает
   сигналы о связках, heartbeat и служебные сообщения через send_html().

2. ВХОДЯЩИЕ запросы «по кнопке/команде» — TelegramCommandListener принимает
   команды (/top, /status, /price BTC) и нажатия inline-кнопок через
   long polling (getUpdates), без вебхуков и открытых портов — идеально
   для Worker-процесса на Railway.

Транспорт построен на httpx (легче и предсказуемее aiogram для уведомлений):
ретраи с экспоненциальным backoff, обработка HTTP 429 (flood control с
параметром retry_after), временные сетевые сбои и «мёртвые» chat_id.

Если TELEGRAM_BOT_TOKEN / CHAT_ID не заданы — notifier переходит в режим
DRY-RUN: сообщения печатаются в лог (удобно для локальной разработки).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

import httpx

log = logging.getLogger("telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4000  # лимит Telegram API — 4096, оставляем запас
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3

#: Обработчик команды: async (chat_id, args) -> HTML-ответ (или None).
CommandHandler = Callable[[str, str], Awaitable[Optional[str]]]

#: Главное inline-меню — крепится к сигналам и ответам на команды.
#: Сигналы только по запросу: топ, сигнал, монеты, funding, калькулятор.
MAIN_MENU_KEYBOARD: dict[str, Any] = {
    "inline_keyboard": [
        [
            {"text": "📊 Топ S→F", "callback_data": "top"},
            {"text": "📊 Топ F→S", "callback_data": "top:fs"},
            {"text": "🎯 Сигнал", "callback_data": "signal"},
        ],
        [
            {"text": "🪙 Монеты", "callback_data": "coins"},
            {"text": "💠 Разбор BTC", "callback_data": "price:BTC"},
            {"text": "💠 Разбор ETH", "callback_data": "price:ETH"},
        ],
        [
            {"text": "💰 Funding BTC", "callback_data": "funding:BTC"},
            {"text": "🧮 Кальк BTC $1000", "callback_data": "calc:BTC 1000"},
        ],
        [
            {"text": "📩 События", "callback_data": "signals"},
            {"text": "🏦 Биржи", "callback_data": "exchanges"},
        ],
        [
            {"text": "📚 Гайд", "callback_data": "guide"},
            {"text": "🧠 Стратегия", "callback_data": "strategy"},
            {"text": "🩺 Статус", "callback_data": "status"},
        ],
        [
            {"text": "❓ Помощь", "callback_data": "help"},
        ],
    ]
}


class TelegramNotifier:
    """Отправка HTML-сообщений + приём обновлений через Telegram Bot API."""

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
                "сообщения будут печататься только в лог"
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

    # ------------------------------------------------------------------ исходящие
    async def send_html(self, html: str, *, reply_markup: Optional[dict] = None) -> bool:
        """Отправить HTML-сообщение во все настроенные чаты. True — доставлено везде."""
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
            if await self._send_to_chat(chat_id, text, reply_markup=reply_markup):
                at_least_one_sent = True
        return at_least_one_sent

    async def send_html_to_chat(
        self,
        chat_id: str,
        html: str,
        *,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Ответ конкретному чату (используется для ответов на команды)."""
        if self.dry_run:
            preview = html.replace("\n", " | ")
            log.info("[DRY-RUN] Ответ чату %s: %s", chat_id, preview[:300])
            return True
        if self._client is None:
            await self.start()
        assert self._client is not None
        text = html if len(html) <= MAX_MESSAGE_LENGTH else html[:MAX_MESSAGE_LENGTH]
        return await self._send_to_chat(chat_id, text, reply_markup=reply_markup)

    # ------------------------------------------------------------------ входящие
    async def get_updates(
        self,
        offset: int = 0,
        timeout: float = 25.0,
        allowed_updates: Sequence[str] = ("message", "callback_query"),
    ) -> list[dict[str, Any]]:
        """Long polling Telegram: возвращает список накопленных updates."""
        if not self._bot_token:
            raise RuntimeError("get_updates недоступен: не задан TELEGRAM_BOT_TOKEN")
        if self._client is None:
            await self.start()
        assert self._client is not None

        response = await self._client.post(
            f"/bot{self._bot_token}/getUpdates",
            json={
                "offset": offset,
                "timeout": int(timeout),
                "allowed_updates": list(allowed_updates),
            },
            # таймаут запроса чуть больше периода long polling
            timeout=httpx.Timeout(timeout + 10.0),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"getUpdates HTTP {response.status_code}: {self._safe_body(response)}"
            )
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"getUpdates отклонён: {payload.get('description', '?')}")
        return payload.get("result") or []

    async def answer_callback_query(
        self, callback_query_id: Optional[str], text: Optional[str] = None
    ) -> None:
        """Подтверждает нажатие кнопки (убирает «часики» в клиенте)."""
        if not callback_query_id or not self._bot_token:
            return
        if self._client is None:
            await self.start()
        assert self._client is not None
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:190]
        try:
            await self._client.post(
                f"/bot{self._bot_token}/answerCallbackQuery",
                json=payload,
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 — подтверждение некритично
            log.debug("answerCallbackQuery не удался: %s", exc)

    # ------------------------------------------------------------------ internals
    async def _send_to_chat(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        url = f"/bot{self._bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

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


class TelegramCommandListener:
    """
    Приём команд и нажатий inline-кнопок через long polling (getUpdates).

    Маршрутизирует «/команду» или callback_data к обработчику из словаря
    handlers и отправляет ответ с главным меню-кнопками. Сообщения от чатов,
    которых нет в CHAT_ID, игнорируются (или подтверждаются «нет доступа»).
    """

    def __init__(
        self,
        notifier: TelegramNotifier,
        handlers: dict[str, CommandHandler],
        allowed_chat_ids: Sequence[str],
        *,
        poll_timeout: float = 25.0,
    ) -> None:
        self._notifier = notifier
        self._handlers = handlers
        self._allowed_chats = {str(chat_id) for chat_id in allowed_chat_ids}
        self._poll_timeout = poll_timeout
        self._last_conflict_log = 0.0

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        """Бесконечный цикл long polling; останавливается отменой задачи."""
        log.info(
            "Приём команд Telegram включён (long polling): %s",
            ", ".join(f"/{name}" for name in self._handlers),
        )
        offset = await self._skip_backlog()
        while True:
            try:
                updates = await self._notifier.get_updates(
                    offset=offset, timeout=self._poll_timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — циклон приёма не умирает
                self._log_poll_error(exc)
                await asyncio.sleep(3.0)
                continue

            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    await self._dispatch(update)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка обработки команды Telegram")

    async def _skip_backlog(self) -> int:
        """Пропускает команды, накопившиеся пока бот был выключен."""
        try:
            latest = await self._notifier.get_updates(offset=-1, timeout=0.0)
        except Exception:  # noqa: BLE001
            return 0
        if latest:
            return int(latest[-1]["update_id"]) + 1
        return 0

    # ------------------------------------------------------------------ routing
    async def _dispatch(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._dispatch_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return  # обычный текст игнорируем — слушаем только команды
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if not self._chat_allowed(chat_id):
            log.info("Telegram: команда от постороннего чата %s — игнорирую", chat_id)
            return
        command, args = self._parse_command(text)
        if command is None:
            return
        await self._route(command, args, chat_id)

    async def _dispatch_callback(self, callback_query: dict[str, Any]) -> None:
        chat_id = str(((callback_query.get("message") or {}).get("chat") or {}).get("id", ""))
        callback_id = callback_query.get("id")
        data = callback_query.get("data") or ""
        if not self._chat_allowed(chat_id):
            await self._notifier.answer_callback_query(callback_id, "⛔ Нет доступа")
            return
        command, args = self._parse_callback_data(data)
        # Сразу убираем «часики» на кнопке, затем готовим ответ.
        await self._notifier.answer_callback_query(callback_id)
        await self._route(command, args, chat_id)

    async def _route(self, command: str, args: str, chat_id: str) -> None:
        handler = self._handlers.get(command)
        if handler is None:
            handler = self._handlers.get("help")
            args = ""
        try:
            html = await handler(chat_id, args)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработчика команды /%s", command)
            html = "⚠️ Не удалось выполнить команду, попробуйте позже."
        if html:
            await self._notifier.send_html_to_chat(
                chat_id, html, reply_markup=MAIN_MENU_KEYBOARD
            )

    # ------------------------------------------------------------------ helpers
    def _chat_allowed(self, chat_id: str) -> bool:
        return chat_id in self._allowed_chats

    @staticmethod
    def _parse_command(text: str) -> tuple[Optional[str], str]:
        """"/price BTC@MyBot pepe" → ("price", "BTC pepe")-подобный разбор."""
        parts = text.split(maxsplit=1)
        if not parts:
            return None, ""
        command = parts[0].lstrip("/").split("@", 1)[0].lower()  # /top@BotName → top
        args = parts[1].strip() if len(parts) > 1 else ""
        return (command or None), args

    @staticmethod
    def _parse_callback_data(data: str) -> tuple[str, str]:
        """"price:BTC" → ("price", "BTC"); "top" → ("top", "")."""
        if ":" in data:
            command, args = data.split(":", 1)
            return command.strip(), args.strip()
        return data.strip(), ""

    def _log_poll_error(self, exc: Exception) -> None:
        message = str(exc)
        if "409" in message or "Conflict" in message:
            if time.monotonic() - self._last_conflict_log > 60.0:
                self._last_conflict_log = time.monotonic()
                log.error(
                    "Telegram: конфликт getUpdates (409). Похоже, бот с этим токеном "
                    "запущен в двух местах одновременно."
                )
            return
        log.warning("Telegram: ошибка опроса команд: %s", message[:200])
