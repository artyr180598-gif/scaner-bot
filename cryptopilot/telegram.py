from __future__ import annotations

import asyncio
import html
import math
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, TelegramObject

from cryptopilot import __version__
from cryptopilot.backtest import WalkForwardBacktester
from cryptopilot.config import Settings
from cryptopilot.exchange import ExchangeClient
from cryptopilot.health import RuntimeHealth
from cryptopilot.models import (
    BacktestResult,
    CalibrationStats,
    EarlyScanReport,
    EarlySetup,
    ScanReport,
    Side,
    Signal,
)
from cryptopilot.scanner import MarketScanner
from cryptopilot.storage import SignalStore

SCAN = "🔎 Сканировать рынок"
ANALYZE = "🪙 Анализ монеты"
EARLY = "⚡ До импульса"
BEST = "⭐ Лучшие сигналы"
BACKTEST = "📊 Бэктест"
STATUS = "⚙️ Статус"
PERFORMANCE = "📈 Результаты"
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
            [KeyboardButton(text=EARLY), KeyboardButton(text=BEST)],
            [KeyboardButton(text=BACKTEST), KeyboardButton(text=PERFORMANCE)],
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
            f"<b>CryptoPilot {html.escape(release_label())}</b>\n\n"
            "Я сканирую ликвидные USDT‑фьючерсы, подтверждаю идею на нескольких "
            "таймфреймах и показываю только планы с контролируемым риском. "
            "Автоторговли нет: ордера я не размещаю.\n\n"
            "Нажмите кнопку ниже.",
            reply_markup=main_keyboard(),
        )

    @router.message(Command("menu"))
    async def menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            f"✅ Меню CryptoPilot {html.escape(release_label())} обновлено.",
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

    @router.message(Command("early"))
    @router.message(F.text == EARLY)
    async def early(message: Message, state: FSMContext) -> None:
        await state.clear()
        progress = await message.answer(
            "⏳ Ищу сжатие волатильности до выхода цены из диапазона…"
        )
        try:
            report = await scanner.scan_early_moves()
            await progress.edit_text(format_early_scan(report))
            for setup in report.setups[:3]:
                await message.answer(format_early_setup(setup))
        except Exception as exc:
            health.last_error = str(exc)
            await progress.edit_text(
                f"⚠️ Ранний радар не завершён: {html.escape(str(exc))}"
            )

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
                f"{row['confidence']}/100 · {created:%d.%m %H:%M} UTC"
            )
        await message.answer("\n".join(lines))

    @router.message(Command("performance"))
    @router.message(F.text == PERFORMANCE)
    async def performance(message: Message) -> None:
        overall, longs, shorts, active = await asyncio.gather(
            store.calibration(limit=settings.calibration_lookback),
            store.calibration(side=Side.LONG, limit=settings.calibration_lookback),
            store.calibration(side=Side.SHORT, limit=settings.calibration_lookback),
            store.active_paper_count(),
        )
        await message.answer(
            format_performance(overall, longs, shorts, active, settings.calibration_min_samples)
        )

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
        active_paper = await store.active_paper_count()
        await message.answer(
            "<b>Состояние системы</b>\n"
            f"Версия: <code>{html.escape(release_label())}</code>\n"
            f"Telegram: ✅ @{html.escape(health.bot_username)}\n"
            f"{exchange.name} API: {'✅' if api_ok else '❌'}\n"
            f"Автомониторинг: ✅ каждые {settings.scan_interval_seconds // 60} мин\n"
            f"Автопорог: LONG {settings.min_auto_confidence}/100 · "
            f"SHORT {settings.min_auto_confidence_short}/100\n"
            f"Ранний радар: "
            f"{'авто-наблюдения' if settings.early_auto_alerts else 'только вручную'}\n"
            f"Активных paper-планов: {active_paper}\n"
            f"Последний скан: {last_scan}\n"
            f"Последняя ошибка: {html.escape(health.last_error or scanner.last_error or 'нет')}"
        )

    @router.message(Command("help"))
    @router.message(F.text == HELP)
    async def help_message(message: Message) -> None:
        await message.answer(
            "<b>Как читать сигнал</b>\n"
            "• Качество — внутренний рейтинг факторов от 0 до 100, не вероятность прибыли.\n"
            "• Вероятность появляется отдельно после накопления paper-статистики.\n"
            "• Вход действителен только внутри указанной зоны и до срока истечения.\n"
            "• Стоп нельзя отодвигать после входа. Размер позиции уже ограничен заданным риском.\n"
            "• Ступени 50/30/20 — не мартингейл: добавляться можно только до отмены сценария.\n"
            "• Обычное плечо 1–2x, жёсткий максимум 3x; плечо не повышает допустимый риск.\n"
            "• NO TRADE означает, что подтверждений недостаточно.\n\n"
            "Команды: /menu, /scan, /early, /analyze BTC, /backtest BTC, /best, "
            "/performance, /status.\n\n"
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


def release_label() -> str:
    commit = os.getenv("RAILWAY_GIT_COMMIT_SHA", "").strip()
    return f"{__version__} · {commit[:7]}" if commit else __version__


def format_indicators(signal: Signal) -> str:
    labels = {
        "5": "5m",
        "15": "15m",
        "30": "30m",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "D": "1d",
    }
    lines: list[str] = []
    for timeframe, feature in signal.features.items():
        trend = "↑" if feature.supertrend_direction > 0 else "↓"
        lines.append(
            f"• {labels.get(timeframe, timeframe)}: RSI {feature.rsi14:.1f} · "
            f"ADX {feature.adx14:.1f} · DMI {feature.dmi_spread:+.1f} · "
            f"CMF {feature.cmf20:+.2f} · RVOL {feature.relative_volume20:.2f}× · "
            f"CHOP {feature.choppiness14:.1f} · BB/KC {feature.keltner_squeeze_ratio:.2f} · "
            f"Trend Guard {trend}"
        )
    return "\n".join(lines)


def format_market_context(signal: Signal) -> str:
    values = signal.market_context
    if not values:
        return "• Микроструктура недоступна; решение не опирается на неё"
    pieces = [
        f"funding {values.get('funding_pct', 0):+.3f}%",
        f"spread {values.get('spread_bps', 0):.1f} bps",
    ]
    if "oi_change_pct" in values:
        pieces.append(f"OI {values['oi_change_pct']:+.1f}%")
    if "taker_buy_ratio" in values:
        pieces.append(f"taker buy {values['taker_buy_ratio']:.0%}")
    if "orderbook_imbalance" in values:
        pieces.append(f"book {values['orderbook_imbalance']:+.0%}")
    if "long_short_ratio" in values:
        pieces.append(f"L/S {values['long_short_ratio']:.2f}")
    return "• " + " · ".join(pieces)


def format_signal(signal: Signal) -> str:
    indicators = format_indicators(signal)
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
            + (f"\n\n<b>Индикаторы</b>\n{indicators}" if indicators else "")
            + "\n\nЛучшее действие сейчас — дождаться более чистой конфигурации."
        )

    assert signal.plan is not None
    observation_reasons = []
    if signal.confidence < signal.required_confidence:
        observation_reasons.append("Качество ниже порога торгового сигнала")
    if signal.plan.expires_at <= datetime.now(UTC):
        observation_reasons.append("Срок сценария истёк; нужен новый анализ")
    observation_reasons.extend(signal.blockers)
    if observation_reasons:
        return (
            f"⚪ <b>{html.escape(signal.symbol)} · КАНДИДАТ, НЕ ВХОД</b>\n"
            f"Предполагаемое направление: {signal.side.value} · биржа: {signal.exchange}\n"
            f"Качество: {signal.confidence}/100 · порог: {signal.required_confidence}/100\n"
            + "\n".join(f"• {html.escape(item)}" for item in observation_reasons)
            + f"\n\n{_format_signal_calibration(signal)}"
            + "\n\nБаллы качества не являются вероятностью прибыли."
            + f"\n\n<b>Индикаторы</b>\n{indicators}"
            + "\n\n<b>Риски</b>\n"
            + "\n".join(f"• {html.escape(item)}" for item in signal.risks)
            + "\n\nПлан покупки и доборов не выдан. Дождитесь нового подтверждения."
        )
    icon = "🟢" if signal.side is Side.LONG else "🔴"
    reasons = "\n".join(f"• {html.escape(item)}" for item in signal.reasons)
    risks = (
        "\n".join(f"• {html.escape(item)}" for item in signal.risks)
        or "• Явных дополнительных рисков модель не выделила"
    )
    calibration = _format_signal_calibration(signal)
    return (
        f"{icon} <b>{html.escape(signal.symbol)} · {signal.side.value}</b>\n"
        f"Биржа: {signal.exchange} · BTC regime: {signal.regime}\n"
        f"Качество сетапа: <b>{signal.confidence}/100</b> · "
        f"автопорог {signal.required_confidence}/100\n"
        f"{calibration}\n"
        f"Score: {signal.score:+.1f}/100 · данные: {signal.data_age_seconds // 60} мин назад\n\n"
        f"<b>Торговый план</b>\n"
        f"Зона входа: <code>{price(signal.plan.entry_low)}–{price(signal.plan.entry_high)}</code>\n"
        f"Стоп: <code>{price(signal.plan.stop_loss)}</code>\n"
        f"TP1: <code>{price(signal.plan.take_profit_1)}</code> (1R)\n"
        f"TP2: <code>{price(signal.plan.take_profit_2)}</code> ({signal.plan.risk_reward_2:.1f}R)\n"
        f"TP3: <code>{price(signal.plan.take_profit_3)}</code> (3R)\n"
        f"Действителен до: {signal.plan.expires_at:%d.%m %H:%M} UTC\n"
        "Защитный стоп: не ждать закрытия свечи за уровнем. "
        "Слом структуры до входа отменяет сценарий.\n\n"
        f"<b>Контроль риска</b>\n"
        f"Расчётный объём: ≈ <code>{signal.plan.suggested_notional:.2f} USDT</code> "
        f"({signal.plan.suggested_quantity:.8g} монеты)\n"
        f"Риск по стопу: ≈ <code>{signal.plan.risk_amount:.2f} USDT</code>\n\n"
        "Доборы не предусмотрены: вход только в указанной зоне, без усреднения.\n"
        f"Горизонт: {html.escape(signal.plan.holding_horizon)}\n"
        "Риск расчётный: комиссии, проскальзывание и ценовые разрывы могут увеличить убыток. "
        "Бот не размещает защитные ордера за вас.\n\n"
        f"<b>Почему</b>\n{reasons}\n\n"
        f"<b>Индикаторы по таймфреймам</b>\n{indicators}\n\n"
        f"<b>Деривативы и микроструктура</b>\n{format_market_context(signal)}\n\n"
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
        f"Планов от {manual_threshold}/100: {len(qualified)}\n"
        f"Время: {duration:.1f} сек\n\n"
        + (
            "Ниже отправляю лучшие варианты."
            if qualified
            else "Сейчас чистых сетапов нет — NO TRADE."
        )
    )


def format_early_scan(report: EarlyScanReport) -> str:
    duration = (report.finished_at - report.started_at).total_seconds()
    return (
        "<b>Ранний радар завершён</b>\n"
        f"Биржа: {report.exchange}\n"
        f"Ликвидный universe: {report.universe_count}\n"
        f"Глубоко проверено: {report.analyzed_count}\n"
        f"Сетапов до пробоя: {len(report.setups)}\n"
        f"Время: {duration:.1f} сек\n\n"
        + (
            "Ниже — лучшие сценарии наблюдения. Вход только после триггера."
            if report.setups
            else "Сейчас подтверждённого предимпульсного сжатия нет."
        )
    )


def format_early_setup(setup: EarlySetup) -> str:
    icon = "🟢" if setup.bias is Side.LONG else "🔴"
    reasons = "\n".join(f"• {html.escape(item)}" for item in setup.reasons)
    risks = (
        "\n".join(f"• {html.escape(item)}" for item in setup.risks)
        or "• Дополнительных рисков радар не выделил"
    )
    stage = (
        "ПОДТВЕРЖДЁННОЕ НАБЛЮДЕНИЕ 15m"
        if setup.stage == "CONFIRMED_WATCH"
        else "НАБЛЮДЕНИЕ"
    )
    metrics = setup.metrics
    taker = metrics.get("taker_buy_ratio", -1)
    book = metrics.get("orderbook_imbalance", -2)
    metric_lines = (
        f"• BB/Keltner: {metrics.get('keltner_squeeze_ratio', 0):.2f} · "
        f"squeeze {metrics.get('squeeze_bars', 0):.0f} свеч.\n"
        f"• CHOP14 {metrics.get('choppiness14', 0):.1f} · "
        f"CMF20 {metrics.get('cmf20', 0):+.2f} · "
        f"RVOL {metrics.get('relative_volume20', 0):.2f}×"
        + (f"\n• Taker buy {taker:.0%}" if taker >= 0 else "")
        + (f" · стакан {book:+.0%}" if book >= -1 else "")
        + (
            "\n• Trend Guard: "
            + ("1h ↑" if metrics.get("supertrend_1h", 0) > 0 else "1h ↓")
            + " · "
            + ("4h ↑" if metrics.get("supertrend_4h", 0) > 0 else "4h ↓")
        )
    )
    return (
        f"⚡ {icon} <b>{html.escape(setup.symbol)} — ДО ИМПУЛЬСА</b>\n"
        f"Стадия: <b>{stage}</b>\n"
        f"Предполагаемое направление: <b>{setup.bias.value}</b>\n"
        f"Готовность: <b>{setup.readiness}/100</b> · BTC regime: {setup.regime}\n"
        f"Текущая цена: <code>{price(setup.price)}</code>\n\n"
        "<b>Это наблюдение, а не команда на вход</b>\n"
        f"Активация после закрытия за уровнем: <code>{price(setup.trigger_price)}</code>\n"
        f"Альтернативный выход диапазона: <code>{price(setup.opposite_trigger_price)}</code>\n"
        f"Сценарий сломан около: <code>{price(setup.invalidation_price)}</code>\n"
        f"Истекает: {setup.expires_at:%d.%m %H:%M} UTC\n\n"
        f"<b>Почему движение может готовиться</b>\n{reasons}\n\n"
        f"<b>Доказательства индикаторами</b>\n{metric_lines}\n\n"
        f"<b>Что может отменить сценарий</b>\n{risks}\n\n"
        "⚠️ До подтверждения границы диапазона позицию не открывать."
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


def _format_signal_calibration(signal: Signal) -> str:
    if (
        signal.estimated_success_pct is None
        or signal.success_interval_low is None
        or signal.success_interval_high is None
    ):
        return (
            "Оценка успеха: калибровка собирается "
            f"(закрытых paper-сделок: {signal.calibration_samples})"
        )
    return (
        f"Историческая частота успеха: <b>{signal.estimated_success_pct:.1f}%</b> "
        f"(95% диапазон {signal.success_interval_low:.1f}–"
        f"{signal.success_interval_high:.1f}%, n={signal.calibration_samples}; не гарантия)"
    )


def format_performance(
    overall: CalibrationStats,
    longs: CalibrationStats,
    shorts: CalibrationStats,
    active: int,
    minimum_samples: int,
) -> str:
    def line(label: str, stats: CalibrationStats) -> str:
        if not stats.sample_size:
            return f"• {label}: пока нет закрытых сделок"
        factor = "∞" if math.isinf(stats.profit_factor) else f"{stats.profit_factor:.2f}"
        return (
            f"• {label}: n={stats.sample_size}, win {stats.win_rate:.1f}%, "
            f"expectancy {stats.expectancy_r:+.2f}R, PF {factor}"
        )

    readiness = (
        "✅ достаточно для первичной калибровки"
        if overall.sample_size >= minimum_samples
        else f"⏳ нужно минимум {minimum_samples} закрытых сделок"
    )
    return (
        "<b>Реальная paper-статистика</b>\n"
        f"{line('Все', overall)}\n"
        f"{line('LONG', longs)}\n"
        f"{line('SHORT', shorts)}\n"
        f"• Ожидают входа или закрытия: {active}\n\n"
        f"{readiness}\n"
        "Статистика считается по сигналам, отправленным ботом, с учётом комиссии "
        "и консервативного порядка SL/TP."
    )
