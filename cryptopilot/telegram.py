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
    CURRENT_STRATEGY_VERSION,
    BacktestResult,
    CalibrationStats,
    EarlyScanReport,
    EarlySetup,
    ScanReport,
    Side,
    Signal,
)
from cryptopilot.prime_delivery import refresh_prime_entry
from cryptopilot.scanner import MarketScanner
from cryptopilot.smart_money import SmartMoneyScanner, format_smart_money_setup
from cryptopilot.storage import SignalStore

UNIFIED = "🎯 Единый поиск"
SCAN = "🔎 Лучшие до движения"
ANALYZE = "🪙 Анализ монеты"
EARLY = "⚡ До импульса"
SMART_MONEY = "🐋 Крупный капитал"
PRIME = "🎯 PRIME поиск"
BEST = "⭐ Лучший сейчас"
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
            [KeyboardButton(text=UNIFIED), KeyboardButton(text=ANALYZE)],
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
    smart_money: SmartMoneyScanner | None = None,
) -> Router:
    router = Router(name="cryptopilot")
    router.message.middleware(AuthorizationMiddleware(settings.allowed_chat_ids))
    smart_money = smart_money or SmartMoneyScanner(exchange, settings)
    search_lock = asyncio.Lock()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            f"<b>CryptoPilot {html.escape(release_label())}</b>\n\n"
            "Я сканирую ликвидные USDT‑фьючерсы и во всех основных режимах сначала "
            "ищу подготовку движения ДО разгона цены. Уже пробитые/улетевшие сценарии "
            "понижаются или скрываются. Автоторговли нет: ордера я не размещаю.\n\n"
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

    @router.message(Command("search"))
    @router.message(Command("scan"))
    @router.message(Command("prime"))
    @router.message(Command("smartmoney"))
    @router.message(Command("best"))
    @router.message(
        F.text.in_(
            {UNIFIED, SCAN, PRIME, SMART_MONEY, BEST, "🔎 Сканировать рынок", "⭐ Лучшие сигналы"}
        )
    )
    async def unified_search(message: Message, state: FSMContext) -> None:
        await state.clear()
        if search_lock.locked():
            await message.answer(
                "⏳ Поиск уже идёт. Я не запускаю второй тяжёлый скан параллельно; "
                "результат первого появится автоматически."
            )
            return
        async with search_lock:
            progress = await message.answer(
                "⏳ Единый поиск запущен.\n"
                "1/3 — сканирую ликвидный рынок на подготовку ДО импульса.",
                reply_markup=main_keyboard(),
            )
            scan_task = asyncio.create_task(smart_money.scan())
            started = asyncio.get_running_loop().time()
            report = None
            used_cached = False
            try:
                while report is None:
                    done, _ = await asyncio.wait({scan_task}, timeout=8)
                    if scan_task in done:
                        report = scan_task.result()
                        break
                    elapsed = int(asyncio.get_running_loop().time() - started)
                    preselected = smart_money.flow_watchlist()
                    stage = (
                        "2/3 — глубокая PRIME-проверка лучших кандидатов"
                        if preselected
                        else "1/3 — ранний отбор ещё продолжается"
                    )
                    try:
                        await progress.edit_text(
                            f"⏳ {stage}.\n"
                            f"Прошло {elapsed} сек · под live-наблюдением: "
                            f"{len(preselected)} монет."
                        )
                    except Exception:
                        pass
                    if elapsed >= 36:
                        scan_task.cancel()
                        scan_task.add_done_callback(_consume_background_task)
                        cached = smart_money.last_report
                        if cached is not None:
                            age = (datetime.now(UTC) - cached.finished_at).total_seconds()
                            if 0 <= age <= 300:
                                report = cached
                                used_cached = True
                                break
                        preview = smart_money.flow_watchlist()
                        health.last_error = "manual PRIME search exceeded 36 seconds"
                        text = (
                            "⚠️ Глубокий скан не завершился вовремя, поэтому я не оставляю "
                            "сообщение висеть бесконечно.\n"
                            "Торговый вход не подтверждён."
                        )
                        try:
                            await progress.edit_text(text)
                        except Exception:
                            await message.answer(text)
                        if preview:
                            await message.answer(format_flow_watchlist_preview(preview))
                        return

                health.scans_total += 1
                fresh_age = max(
                    0,
                    int((datetime.now(UTC) - report.finished_at).total_seconds()),
                )
                source = (
                    f"резервный кэш {fresh_age} сек"
                    if used_cached
                    else "свежий скан"
                )
                candidates = tuple(
                    sorted(
                        (item for item in report.setups if item.prime_ready),
                        key=lambda item: (item.prime_score, item.score),
                        reverse=True,
                    )
                )
                summary = (
                    f"🎯 <b>Единый поиск · {report.finished_at:%H:%M:%S} UTC</b>\n"
                    f"Источник: {source} · рынок: {report.universe_count} · "
                    f"глубоко: {report.analyzed_count} · ошибок данных: {len(report.errors)}\n"
                    f"PRIME-ready: {len(candidates)} · PRE-MOVE наблюдений: "
                    f"{len(report.setups)}."
                )
                try:
                    await progress.edit_text(summary)
                except Exception:
                    await message.answer(summary)

                delivered = 0
                for item in candidates[:3]:
                    checked = await refresh_prime_entry(item, exchange, settings)
                    await message.answer(format_prime_setup(checked))
                    delivered += int(checked.prime_ready)

                if delivered:
                    return

                prepare = [
                    item
                    for item in report.setups
                    if not item.prime_ready and item.stage in {"ARMED", "WATCH"}
                ][:3]
                if prepare:
                    await message.answer(
                        "🟡 <b>Строгого PRIME-входа сейчас нет, но бот нашёл "
                        "подготовку ДО импульса.</b>\n"
                        "Это watchlist: ждём усиления OI/Spot/Flow, не догоняем цену."
                    )
                    for item in prepare:
                        await message.answer(format_smart_money_setup(item))
                    return

                preview = smart_money.flow_watchlist()
                if preview:
                    await message.answer(format_flow_watchlist_preview(preview))
                else:
                    await message.answer(
                        "⚪ <b>NO TRADE</b> — сейчас нет достаточно чистой подготовки "
                        "до движения. Бот продолжает фоновый поиск и не подменяет "
                        "отсутствие сетапа поздним сигналом."
                    )
            except TimeoutError:
                health.last_error = "PRIME scan hard deadline"
                text = (
                    "⚠️ PRIME-скан остановлен по жёсткому дедлайну. "
                    "Бот продолжит фоновый поиск; зависшего сообщения больше не будет."
                )
                try:
                    await progress.edit_text(text)
                except Exception:
                    await message.answer(text)
            except Exception as exc:
                health.last_error = type(exc).__name__
                text = (
                    "⚠️ Поиск не завершён. Вход не подтверждён: "
                    + html.escape(type(exc).__name__)
                )
                try:
                    await progress.edit_text(text)
                except Exception:
                    await message.answer(text)

    @router.message(Command("early"))
    @router.message(F.text == EARLY)
    async def early(message: Message, state: FSMContext) -> None:
        await state.clear()
        progress = await message.answer("⏳ Ищу сжатие волатильности до выхода цены из диапазона…")
        try:
            report = await scanner.scan_early_moves()
            await progress.edit_text(format_early_scan(report))
            for setup in report.setups[:3]:
                await message.answer(format_early_setup(setup))
        except Exception as exc:
            health.last_error = str(exc)
            await progress.edit_text(f"⚠️ Ранний радар не завершён: {html.escape(str(exc))}")

    @router.message(Command("analyze"))
    async def analyze_command(message: Message, state: FSMContext) -> None:
        symbol = command_argument(message.text)
        if symbol:
            await run_analysis(message, scanner, smart_money, symbol)
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
        await run_analysis(message, scanner, smart_money, message.text or "")

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
        await message.answer(
            "Введите монету для исторического trend-baseline, например <code>ETH</code>. "
            "Это не полный PRIME 3.6: в свечной истории нет live Spot/book/CVD evidence."
        )

    @router.message(InputState.backtest_symbol)
    async def backtest_input(message: Message, state: FSMContext) -> None:
        await state.clear()
        await run_backtest(message, exchange, message.text or "")

    @router.message(Command("performance"))
    @router.message(F.text == PERFORMANCE)
    async def performance(message: Message) -> None:
        overall, longs, shorts, active = await asyncio.gather(
            store.calibration(
                strategy_version=CURRENT_STRATEGY_VERSION,
                limit=settings.calibration_lookback,
            ),
            store.calibration(
                side=Side.LONG,
                strategy_version=CURRENT_STRATEGY_VERSION,
                limit=settings.calibration_lookback,
            ),
            store.calibration(
                side=Side.SHORT,
                strategy_version=CURRENT_STRATEGY_VERSION,
                limit=settings.calibration_lookback,
            ),
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
        prime_finished = (
            smart_money.last_report.finished_at.strftime('%d.%m %H:%M UTC')
            if smart_money.last_report else 'ещё не было'
        )
        await message.answer(
            "<b>Состояние системы</b>\n"
            f"Версия: <code>{html.escape(release_label())}</code>\n"
            f"Telegram: ✅ @{html.escape(health.bot_username)}\n"
            f"{exchange.name} API: {'✅' if api_ok else '❌'}\n"
            f"PRIME автопоиск: {'✅' if settings.smart_money_auto_scan_enabled else '❌'} "
            f"· интервал {settings.smart_money_scan_interval_seconds} сек\n"
            f"PRIME автоуведомления: {'✅' if settings.prime_alerts_enabled else '❌'} "
            f"· максимум {settings.prime_max_alerts_per_day}/сутки\n"
            f"Последний завершённый PRIME-скан: "
            f"{prime_finished}\n"
            f"Фоновый контроль: ✅ каждые {settings.scan_interval_seconds // 60} мин\n"
            f"Обычные trend-auto алерты: "
            f"{'✅' if settings.standard_auto_alerts_enabled else '❌ (PRIME-first)'}\n"
            f"PRE-MOVE режим основного скана: "
            f"{'✅' if settings.main_scan_premove_only else '❌'}\n"
            f"Ранний радар: "
            f"{'авто-наблюдения' if settings.early_auto_alerts else 'только вручную'}\n"
            f"PRIME: cross-exchange "
            f"{'обязателен' if settings.prime_cross_exchange_required else 'дополнительный'} · "
            f"Shadow {'✅' if settings.prime_shadow_enabled else '❌'}\n"
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
            "• В PRIME-плане усреднение не предусмотрено.\n"
            "• Обычное плечо 1–2x, жёсткий максимум 3x; плечо не повышает допустимый риск.\n"
            "• Уже разогнанная цена не превращается в сигнал: "
            "такой сценарий блокируется как поздний.\n"
            "• NO TRADE означает, что подтверждений недостаточно.\n\n"
            "Команды: /menu, /search, /scan, /early, /smartmoney, /prime, /analyze BTC, "
            "/backtest BTC, /best, /primestats, "
            "/performance, /status.\n\n"
            "⚠️ Это аналитическая система, а не персональная финансовая рекомендация. "
            "Фьючерсы могут привести к полной потере капитала.",
            reply_markup=main_keyboard(),
        )

    return router


async def run_analysis(
    message: Message,
    scanner: MarketScanner,
    smart_money: SmartMoneyScanner,
    raw_symbol: str,
) -> None:
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        await message.answer("Не удалось распознать тикер. Пример: <code>BTC</code>.")
        return
    progress = await message.answer(
        f"⏳ PRE-MOVE анализ <b>{html.escape(symbol)}</b>: сначала проверяю PRIME/Smart Money…"
    )
    try:
        smart_setup = await smart_money.analyze_symbol(symbol)
        if smart_setup is not None and smart_setup.prime_ready:
            checked = await refresh_prime_entry(
                smart_setup, smart_money.exchange, smart_money.settings
            )
            await progress.edit_text(format_prime_setup(checked))
        else:
            await progress.edit_text(
                f"⚪ <b>{html.escape(symbol)} · NO TRADE</b>\n"
                "Строгий PRIME-вход не подтверждён."
                + (
                    "\n"
                    + "\n".join(
                        "• " + html.escape(reason) for reason in smart_setup.prime_blockers[:5]
                    )
                    if smart_setup is not None
                    else ""
                )
            )
    except Exception as exc:
        await progress.edit_text(f"⚠️ Анализ не завершён: {html.escape(str(exc))}")


async def run_backtest(message: Message, exchange: ExchangeClient, raw_symbol: str) -> None:
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        await message.answer("Не удалось распознать тикер. Пример: <code>BTC</code>.")
        return
    progress = await message.answer(f"⏳ Исторический trend-baseline <b>{html.escape(symbol)}</b>…")
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
    premove = signal.market_context.get("premove_readiness")
    trigger = signal.market_context.get("premove_trigger")
    trigger_distance = signal.market_context.get("premove_trigger_distance_pct")
    premove_line = ""
    if premove is not None and trigger is not None and trigger_distance is not None:
        premove_line = (
            f"PRE-MOVE: <b>{premove:.0f}/100</b> · "
            f"trigger <code>{price(trigger)}</code> · до него {trigger_distance:.2f}%\n"
        )
    return (
        f"{icon} <b>{html.escape(signal.symbol)} · {signal.side.value}</b>\n"
        f"Биржа: {signal.exchange} · BTC regime: {signal.regime}\n"
        f"{premove_line}"
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
        "<b>PRE-MOVE сканирование завершено</b>\n"
        f"Биржа: {report.exchange}\n"
        f"Ликвидный universe: {report.universe_count}\n"
        f"Глубоко проверено: {report.analyzed_count}\n"
        f"Планов от {manual_threshold}/100: {len(qualified)}\n"
        f"Время: {duration:.1f} сек\n\n"
        + (
            "Ниже отправляю только варианты, где основной импульс ещё не должен быть реализован."
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
            "Ниже — сценарии, где цена ещё не должна была пробить 15m структуру."
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
        "ARMED PRE-MOVE — ДО 15m ПРОБОЯ" if setup.stage == "ARMED_PREMOVE" else "РАННЕЕ НАБЛЮДЕНИЕ"
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
        + (
            f"\n• До 1h trigger: {metrics.get('trigger_distance_pct', 0):.2f}%"
            if "trigger_distance_pct" in metrics
            else ""
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


def format_prime_setup(item) -> str:
    if not item.prime_ready or item.plan is None or item.plan.expires_at <= datetime.now(UTC):
        reasons = item.prime_blockers or ("Подтверждённого актуального плана нет",)
        return (
            f"⚪ <b>{html.escape(item.symbol)} · НАБЛЮДАТЬ, НЕ ВХОДИТЬ</b>\n"
            + "\n".join("• " + html.escape(reason) for reason in reasons[:5])
            + "\nПлан входа не выдан. Нужна новая проверка."
        )
    side_icon = "🟢" if item.bias is Side.LONG else "🔴"
    reasons = (
        "\n".join(f"• {html.escape(value)}" for value in item.prime_reasons)
        or "• Совпали строгие pre-move фильтры"
    )

    spot_parts: list[str] = []
    if item.spot_taker_buy_ratio is not None:
        spot_parts.append(f"spot BUY {item.spot_taker_buy_ratio:.0%}")
    if item.spot_orderbook_imbalance is not None:
        spot_parts.append(f"book {item.spot_orderbook_imbalance:+.0%}")
    if item.spot_block_trade_notional is not None:
        spot_parts.append(f"block ${item.spot_block_trade_notional:,.0f}")
    if item.spot_perp_basis_bps is not None:
        spot_parts.append(f"perp/spot {item.spot_perp_basis_bps:+.1f} bps")
    spot_line = " · ".join(spot_parts) if spot_parts else "spot-подтверждение недоступно"

    live_parts: list[str] = []
    if item.live_delta_ratio_60s is not None:
        live_parts.append(f"Δ60s {item.live_delta_ratio_60s:+.0%}")
    if item.live_cvd_ratio_5m is not None:
        live_parts.append(f"CVD5m {item.live_cvd_ratio_5m:+.0%}")
    if item.live_volume_burst_ratio is not None:
        live_parts.append(f"burst {item.live_volume_burst_ratio:.2f}×")
    if item.live_oi_acceleration_pct_per_min is not None:
        live_parts.append(f"OI accel {item.live_oi_acceleration_pct_per_min:+.3f}%/мин")
    live_line = " · ".join(live_parts) if live_parts else "основной live-поток ещё не разогнан"
    if item.bias is Side.LONG:
        wall_ratio = item.bid_wall_ratio
        wall_seconds = item.bid_wall_persistence_seconds
        replenishment = item.bid_replenishment_usdt_60s
    else:
        wall_ratio = item.ask_wall_ratio
        wall_seconds = item.ask_wall_persistence_seconds
        replenishment = item.ask_replenishment_usdt_60s
    liquidity_line = (
        f"wall {wall_ratio:.1f}×/{wall_seconds:.0f}с · replenishment ${replenishment:,.0f}/60с"
        if wall_ratio is not None
        else "устойчивая liquidity-wall пока не подтверждена"
    )
    cross_line = (
        f"{item.cross_exchange}: {item.cross_confirmations} подтвержд. · "
        f"конфликтов {item.cross_conflicts}"
        + (
            f" · расхождение цены {item.cross_price_divergence_bps:.1f} bps"
            if item.cross_price_divergence_bps is not None
            else ""
        )
        if item.cross_exchange
        else "вторая биржа временно недоступна"
    )
    plan = item.plan
    if plan is not None:
        action = "ПОКУПАТЬ" if item.bias is Side.LONG else "ОТКРЫВАТЬ SHORT"
        exit_action = "продажа" if item.bias is Side.LONG else "откуп SHORT"
        plan_text = (
            f"<b>PRIME торговый план</b>\n"
            f"{action}: <code>{price(plan.entry_low)}–{price(plan.entry_high)}</code>\n"
            f"Стоп: <code>{price(plan.stop_loss)}</code>\n"
            f"TP1: <code>{price(plan.take_profit_1)}</code> · "
            f"TP2: <code>{price(plan.take_profit_2)}</code> · "
            f"TP3: <code>{price(plan.take_profit_3)}</code>\n"
            f"TP — это {exit_action}; TP2 R/R после издержек: "
            f"<b>{plan.risk_reward_2:.2f}</b>\n"
            f"План входа действителен до: {plan.expires_at:%d.%m %H:%M} UTC\n"
            f"Риск-модель: до ${plan.risk_amount:.2f} · "
            f"ориентир позиции ${plan.suggested_notional:.0f} · "
            f"плечо до {plan.recommended_leverage}x\n"
            "Не усреднять после выхода из зоны и не догонять цену после trigger."
        )
    else:
        plan_text = (
            "<b>PRIME торговый план</b>\n"
            "Точный вход не выдан: условия для безопасной зоны входа не прошли."
        )
    tradingview_url = (
        f"https://www.tradingview.com/symbols/{item.symbol}.P/?exchange={item.exchange}"
    )

    return (
        f"🎯 {side_icon} <b>{html.escape(item.symbol)} · PRIME PRE-MOVE</b>\n"
        f"Сценарий: <b>{item.bias.value}</b> · Prime score: <b>{item.prime_score}/100</b>\n"
        f"Цена: <code>{price(item.price)}</code> · "
        f"trigger: <code>{price(item.trigger_price)}</code>\n"
        f"До trigger: {item.distance_to_trigger_pct:.2f}% · "
        f"движение ~15м: {item.recent_move_15m_pct:+.2f}%\n"
        f"Структура: 15m {html.escape(item.structure_15m)} · "
        f"1h {html.escape(item.structure_1h)} · 4h {html.escape(item.structure_4h)}\n"
        f"RVOL {item.rvol:.2f}× · funding {item.funding_pct:+.3f}%\n"
        f"Spot: {html.escape(spot_line)}\n"
        f"Liquidity: {html.escape(liquidity_line)}\n"
        f"Liquidations 60с: LONG ${item.long_liquidation_usdt_60s:,.0f} · "
        f"SHORT ${item.short_liquidation_usdt_60s:,.0f}\n"
        f"Cross-exchange: {html.escape(cross_line)}\n"
        f"Live: {html.escape(live_line)}\n\n"
        f"{plan_text}\n\n"
        f"<b>Почему PRIME</b>\n{reasons}\n\n"
        f'<a href="{tradingview_url}">📊 Открыть этот контракт в TradingView</a>\n\n'
        "Это ранний кандидат до очевидного импульса. Prime score — рейтинг качества, "
        "не вероятность прибыли и не гарантия входа крупных денег."
    )


def format_backtest(result: BacktestResult) -> str:
    profit_factor = "∞" if math.isinf(result.profit_factor) else f"{result.profit_factor:.2f}"
    return (
        f"<b>Исторический trend-baseline: {html.escape(result.symbol)}</b>\n"
        f"Период: {result.started_at:%d.%m.%Y}–{result.finished_at:%d.%m.%Y}\n"
        f"Таймфрейм: {result.timeframe} · свечей: {result.bars}\n"
        f"Сделок: {result.trades} · W/L: {result.wins}/{result.losses}\n"
        f"Win rate: <b>{result.win_rate:.1f}%</b>\n"
        f"Expectancy: <b>{result.expectancy_r:+.2f}R</b>\n"
        f"Profit factor: {profit_factor}\n"
        f"Max drawdown: {result.max_drawdown_r:.2f}R\n\n"
        "Тест использует только прошлые закрытые свечи, вход на следующем open, "
        "консервативный порядок SL/TP и поправку на комиссии/проскальзывание. "
        "Этот тест не симулирует PRIME 3.6 live Spot/order-book/CVD/OI стек. "
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
