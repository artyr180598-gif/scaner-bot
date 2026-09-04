from __future__ import annotations

import html
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, TelegramObject

from cryptopilot.backtest import WalkForwardBacktester
from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient
from cryptopilot.health import RuntimeHealth
from cryptopilot.models import BacktestResult, ScanReport, Side, Signal
from cryptopilot.scanner import MarketScanner
from cryptopilot.storage import SignalStore

SCAN = "🔎 Сканировать рынок"
ANALYZE = "🪙 Анализ монеты"
BEST = "⭐ Лучшие сигналы"
BACKTEST = "📊 Бэктест"
STATUS = "⚙️ Статус"
HELP = "❓ Помощь"


class InputState(StatesGroup):
    analysis_symbol = State()
    backtest_symbol = State()


class AuthorizationMiddleware(BaseMiddleware):
    def __init__(self, allowed_ids: frozenset[int]) -> None:
        self.allowed_ids = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable[object]],
        event: TelegramObject,
        data: dict,
    ) -> object:
        message = event if isinstance(event, Message) else None
        user_id = message.from_user.id if message and message.from_user else None
        chat_id = message.chat.id if message else None
        if self.allowed_ids and (user_id in self.allowed_ids or chat_id in self.allowed_ids):
            return await handler(event, data)
        if message:
            if not self.allowed_ids:
                await message.answer(
                    "⛔ Доступ закрыт: добавьте TELEGRAM_CHAT_ID в Railway Variables "
                    "и перезапустите сервис."
                )
            else:
                await message.answer("⛔ Этот Telegram ID не имеет доступа к боту.")
        return None


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SCAN), KeyboardButton(text=ANALYZE)],
            [KeyboardButton(text=BEST), KeyboardButton(text=BACKTEST)],
            [KeyboardButton(text=STATUS), KeyboardButton(text=HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def build_router(
    scanner: MarketScanner,
    exchange: ExchangeClient,
    store: SignalStore,
    settings: Settings,
    health: RuntimeHealth,
) -> Router:
    router = Router(name="cryptopilot")
    router.message.middleware(AuthorizationMiddleware(settings.allowed_chat_ids))

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "<b>CryptoPilot 2.0</b>\n\n"
            "Я сканирую ликвидные USDT‑фьючерсы, подтверждаю идею на нескольких "
            "таймфреймах и показываю только планы с контролируемым риском. "
            "Автоторговли нет: ордера я не размещаю.\n\n"
            "Нажмите кнопку ниже.",
            reply_markup=main_keyboard(),
        )

    @router.message(Command("scan"))
    @router.message(F.text == SCAN)
    async def scan(message: Message, state: FSMContext) -> None:
        await state.clear()
        progress = await message.answer("⏳ Проверяю ликвидность и делаю многофакторный анализ…")
        try:
            report = await scanner.scan_market()
            health.scans_total += 1
            await progress.edit_text(format_scan(report, settings.min_manual_confidence))
            for signal in report.signals[:3]:
                if signal.confidence >= settings.min_manual_confidence:
                    await message.answer(format_signal(signal))
        except Exception as exc:
            health.last_error = str(exc)
            await progress.edit_text(f"⚠️ Сканирование не завершено: {html.escape(str(exc))}")

    @router.message(Command("analyze"))
    async def analyze_command(message: Message, state: FSMContext) -> None:
        symbol = command_argument(message.text)
        if symbol:
            await run_analysis(message, scanner, symbol)
        else:
            await state.set_state(InputState.analysis_symbol)
            await message.answer(
                "Введите монету, например <code>BTC</code> или <code>SOLUSDT</code>:"
            )

    @router.message(F.text == ANALYZE)
    async def analyze_prompt(message: Message, state: FSMContext) -> None:
        await state.set_state(InputState.analysis_symbol)
        await message.answer("Введите монету, например <code>BTC</code> или <code>SOLUSDT</code>:")

    @router.message(InputState.analysis_symbol)
    async def analyze_input(message: Message, state: FSMContext) -> None:
        await state.clear()
        await run_analysis(message, scanner, message.text or "")

    @router.message(Command("backtest"))
    async def backtest_command(message: Message, state: FSMContext) -> None:
        symbol = command_argument(message.text)
        if symbol:
            await run_backtest(message, exchange, symbol)
        else:
            await state.set_state(InputState.backtest_symbol)
            await message.answer(
                "Введите монету для walk-forward теста, например <code>ETH</code>:"
            )

    @router.message(F.text == BACKTEST)
    async def backtest_prompt(message: Message, state: FSMContext) -> None:
        await state.set_state(InputState.backtest_symbol)
        await message.answer("Введите монету для walk-forward теста, например <code>ETH</code>:")

    @router.message(InputState.backtest_symbol)
    async def backtest_input(message: Message, state: FSMContext) -> None:
        await state.clear()
        await run_backtest(message, exchange, message.text or "")

    @router.message(Command("best"))
    @router.message(F.text == BEST)
    async def best(message: Message) -> None:
        rows = await store.recent(8, actionable_only=True)
        if not rows:
            await message.answer("Пока нет сохранённых торговых планов. Запустите сканирование.")
            return
        lines = ["<b>Последние найденные планы</b>"]
        for row in rows:
            icon = "🟢" if row["side"] == "LONG" else "🔴"
            created = datetime.fromisoformat(row["created_at"]).astimezone(UTC)
            lines.append(
                f"{icon} <b>{html.escape(row['symbol'])}</b> {row['side']} · "
                f"{row['confidence']}% · {created:%d.%m %H:%M} UTC"
            )
        await message.answer("\n".join(lines))

    @router.message(Command("status"))
    @router.message(F.text == STATUS)
    async def status(message: Message) -> None:
        try:
            api_ok = await exchange.ping()
        except Exception as exc:
            api_ok = False
            health.last_error = str(exc)
        last = scanner.last_report
        last_scan = last.finished_at.strftime("%d.%m.%Y %H:%M UTC") if last else "ещё не было"
        await message.answer(
            "<b>Состояние системы</b>\n"
            f"Telegram: ✅ @{html.escape(health.bot_username)}\n"
            f"{exchange.name} API: {'✅' if api_ok else '❌'}\n"
            f"Автомониторинг: ✅ каждые {settings.scan_interval_seconds // 60} мин\n"
            f"Автопорог: {settings.min_auto_confidence}%\n"
            f"Последний скан: {last_scan}\n"
            f"Последняя ошибка: {html.escape(health.last_error or scanner.last_error or 'нет')}"
        )

    @router.message(Command("help"))
    @router.message(F.text == HELP)
    async def help_message(message: Message) -> None:
        await message.answer(
            "<b>Как читать сигнал</b>\n"
            "• Уверенность — внутренний рейтинг согласованности факторов, не гарантия прибыли.\n"
            "• Вход действителен только внутри указанной зоны и до срока истечения.\n"
            "• Стоп нельзя отодвигать после входа. Размер позиции уже ограничен заданным риском.\n"
            "• NO TRADE означает, что подтверждений недостаточно.\n\n"
            "Команды: /scan, /analyze BTC, /backtest BTC, /best, /status.\n\n"
            "⚠️ Это аналитическая система, а не персональная финансовая рекомендация. "
            "Фьючерсы могут привести к полной потере капитала.",
            reply_markup=main_keyboard(),
        )

    return router


async def run_analysis(message: Message, scanner: MarketScanner, raw_symbol: str) -> None:
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        await message.answer("Не удалось распознать тикер. Пример: <code>BTC</code>.")
        return
    progress = await message.answer(f"⏳ Анализирую <b>{html.escape(symbol)}</b>…")
    try:
        signal = await scanner.analyze_symbol(symbol)
        await progress.edit_text(format_signal(signal))
    except Exception as exc:
        await progress.edit_text(f"⚠️ Анализ не завершён: {html.escape(str(exc))}")


async def run_backtest(message: Message, exchange: ExchangeClient, raw_symbol: str) -> None:
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        await message.answer("Не удалось распознать тикер. Пример: <code>BTC</code>.")
        return
    progress = await message.answer(f"⏳ Walk-forward тест <b>{html.escape(symbol)}</b>…")
    try:
        candles = await exchange.candles(symbol, "60", 1000)
        result = WalkForwardBacktester().run(symbol, "1h", candles)
        await progress.edit_text(format_backtest(result))
    except Exception as exc:
        await progress.edit_text(f"⚠️ Бэктест не завершён: {html.escape(str(exc))}")


def normalize_symbol(value: str) -> str:
    clean = "".join(character for character in value.upper() if character.isalnum())
    if not clean:
        return ""
    return clean if clean.endswith("USDT") else f"{clean}USDT"


def command_argument(text: str | None) -> str:
    parts = (text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if value >= 0.01:
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_signal(signal: Signal) -> str:
    if signal.side is Side.NO_TRADE:
        blockers = (
            "\n".join(f"• {html.escape(item)}" for item in signal.blockers)
            or "• Нет полного набора подтверждений"
        )
        reasons = "\n".join(f"• {html.escape(item)}" for item in signal.reasons[:3])
        return (
            f"⚪ <b>{html.escape(signal.symbol)} — NO TRADE</b>\n"
            f"Биржа: {signal.exchange} · цена: <code>{price(signal.price)}</code>\n"
            f"Score: {signal.score:+.1f}/100 · BTC regime: {signal.regime}\n\n"
            f"<b>Почему пропускаем</b>\n{blockers}"
            + (f"\n\n<b>Что всё же видно</b>\n{reasons}" if reasons else "")
            + "\n\nЛучшее действие сейчас — дождаться более чистой конфигурации."
        )

    assert signal.plan is not None
    icon = "🟢" if signal.side is Side.LONG else "🔴"
    reasons = "\n".join(f"• {html.escape(item)}" for item in signal.reasons)
    risks = (
        "\n".join(f"• {html.escape(item)}" for item in signal.risks)
        or "• Явных дополнительных рисков модель не выделила"
    )
    return (
        f"{icon} <b>{html.escape(signal.symbol)} · {signal.side.value}</b>\n"
        f"Биржа: {signal.exchange} · BTC regime: {signal.regime}\n"
        f"Уверенность модели: <b>{signal.confidence}%</b> (не гарантия)\n"
        f"Score: {signal.score:+.1f}/100 · данные: {signal.data_age_seconds // 60} мин назад\n\n"
        f"<b>Торговый план</b>\n"
        f"Зона входа: <code>{price(signal.plan.entry_low)}–{price(signal.plan.entry_high)}</code>\n"
        f"Стоп: <code>{price(signal.plan.stop_loss)}</code>\n"
        f"TP1: <code>{price(signal.plan.take_profit_1)}</code> (1R)\n"
        f"TP2: <code>{price(signal.plan.take_profit_2)}</code> ({signal.plan.risk_reward_2:.1f}R)\n"
        f"TP3: <code>{price(signal.plan.take_profit_3)}</code> (3R)\n"
        f"Действителен до: {signal.plan.expires_at:%d.%m %H:%M} UTC\n"
        f"Отмена: {html.escape(signal.plan.invalidation)}\n\n"
        f"<b>Контроль риска</b>\n"
        f"Расчётный объём: ≈ <code>{signal.plan.suggested_notional:.2f} USDT</code> "
        f"({signal.plan.suggested_quantity:.8g} монеты)\n"
        f"Риск по стопу: ≈ <code>{signal.plan.risk_amount:.2f} USDT</code>\n\n"
        f"<b>Почему</b>\n{reasons}\n\n"
        f"<b>Риски</b>\n{risks}\n\n"
        "⚠️ Не входите, если цена уже вне зоны. Не отодвигайте стоп."
    )


def format_scan(report: ScanReport, manual_threshold: int) -> str:
    qualified = [item for item in report.signals if item.confidence >= manual_threshold]
    duration = (report.finished_at - report.started_at).total_seconds()
    return (
        "<b>Сканирование завершено</b>\n"
        f"Биржа: {report.exchange}\n"
        f"Ликвидный universe: {report.universe_count}\n"
        f"Глубоко проверено: {report.analyzed_count}\n"
        f"Планов от {manual_threshold}%: {len(qualified)}\n"
        f"Время: {duration:.1f} сек\n\n"
        + (
            "Ниже отправляю лучшие варианты."
            if qualified
            else "Сейчас чистых сетапов нет — NO TRADE."
        )
    )


def format_backtest(result: BacktestResult) -> str:
    profit_factor = "∞" if math.isinf(result.profit_factor) else f"{result.profit_factor:.2f}"
    return (
        f"<b>Walk-forward: {html.escape(result.symbol)}</b>\n"
        f"Период: {result.started_at:%d.%m.%Y}–{result.finished_at:%d.%m.%Y}\n"
        f"Таймфрейм: {result.timeframe} · свечей: {result.bars}\n"
        f"Сделок: {result.trades} · W/L: {result.wins}/{result.losses}\n"
        f"Win rate: <b>{result.win_rate:.1f}%</b>\n"
        f"Expectancy: <b>{result.expectancy_r:+.2f}R</b>\n"
        f"Profit factor: {profit_factor}\n"
        f"Max drawdown: {result.max_drawdown_r:.2f}R\n\n"
        "Тест использует только прошлые закрытые свечи, вход на следующем open, "
        "консервативный порядок SL/TP и поправку на комиссии/проскальзывание. "
        "Прошлая статистика не гарантирует будущий результат."
    )
