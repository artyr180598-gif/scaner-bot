"""
directional_service.py — «мозг ответов» направленного анализа для Telegram.

Что здесь есть и чего нет:
  * ЕСТЬ: кеш снапшотов, скан вселенной монет, объединение направленного
    сигнала с нейтральным (carry/арбитраж) контекстом, watchlist, профили
    риска по чатам, журнал точности, обработчики команд/кнопок.
  * НЕТ: расчёта индикаторов (это `directional.py`), похода в сеть
    (`market_data.py`) и HTML-вёрстки (`directional_view.py`).

Объединение двух ядер (это ключевое отличие v4):
  У проекта уже есть рыночно-НЕЙТРАЛЬНОЕ ядро (`strategy.py`: спред спот↔перп,
  carry на funding). Направленный сигнал и нейтральная связка — это два разных
  способа заработать на одной монете. Сервис показывает их вместе:
      • есть направленный вход → карточка сделки + (если рядом сильная связка)
        строка «есть и нейтральная альтернатива»;
      • направленного входа нет, а связка есть → честно предлагаем связку
        вместо тишины;
      • нет ни того, ни другого → блок «почему НЕ вход».
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

import directional_view as view
from directional import (
    DEFAULT_PROFILE,
    RISK_PROFILES,
    DirectionalConfig,
    DirectionalSignal,
    RiskProfile,
    analyze,
)
from market_data import MarketDataProvider, MarketSnapshot
from signal_history import SignalHistory

log = logging.getLogger("directional")

__all__ = ["CarryContext", "DirectionalService"]

#: Функция, которой сервис спрашивает у арбитражного ядра контекст по монете.
CarryLookup = Callable[[str], Optional["CarryContext"]]


@dataclass
class CarryContext:
    """Нейтральная (арбитраж/carry) возможность по той же монете."""

    headline: str                 # короткая строка для карточки
    net_percent: float            # ожидаемый чистый итог связки, %
    confidence: float             # 0..100 из квант-ядра
    actionable: bool = False

    def as_hint(self) -> str:
        prefix = "💡 Альтернатива без направления: " if self.actionable else "ℹ️ Нейтральный контекст: "
        return f"{prefix}{self.headline}"


@dataclass
class _CacheEntry:
    signal: DirectionalSignal
    snapshot: MarketSnapshot
    at: float


class DirectionalService:
    """Сервис направленного анализа: кеш, скан, watchlist, ответы бота."""

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        config: DirectionalConfig | None = None,
        history: SignalHistory | None = None,
        default_profile: str = DEFAULT_PROFILE,
        cache_seconds: float = 45.0,
        universe_size: int = 40,
        scan_concurrency: int = 6,
        candles_limit: int = 300,
        carry_lookup: Optional[CarryLookup] = None,
    ) -> None:
        self.provider = provider
        self.cfg = config or DirectionalConfig()
        self.history = history or SignalHistory()
        self.default_profile = default_profile
        self.cache_seconds = cache_seconds
        self.universe_size = universe_size
        self.candles_limit = candles_limit
        self.carry_lookup = carry_lookup
        self._sem = asyncio.Semaphore(scan_concurrency)
        self._cache: dict[str, _CacheEntry] = {}
        self._profiles: dict[str, str] = {}          # chat_id → profile key
        self._watchlist: dict[str, set[str]] = {}    # chat_id → {BASE}
        self._alert_sent: dict[tuple[str, str], float] = {}
        self._alerts_log: list[str] = []
        self.stats = {"analyses": 0, "scans": 0, "cache_hits": 0, "errors": 0}

    # ------------------------------------------------------------------ профили
    def profile_for(self, chat_id: str) -> RiskProfile:
        key = self._profiles.get(str(chat_id), self.default_profile)
        return RISK_PROFILES.get(key, RISK_PROFILES[DEFAULT_PROFILE])

    def set_profile(self, chat_id: str, key: str) -> RiskProfile:
        if key in RISK_PROFILES:
            self._profiles[str(chat_id)] = key
        return self.profile_for(chat_id)

    # ------------------------------------------------------------------- анализ
    async def analyze_base(
        self,
        base: str,
        profile: RiskProfile,
        *,
        force: bool = False,
        now: Optional[float] = None,
    ) -> DirectionalSignal:
        """
        Полный анализ монеты. Данные кешируются на `cache_seconds`, но САМ
        анализ пересчитывается всегда — профиль риска меняет ворота, а не
        рынок, поэтому кешируем именно снапшот.
        """
        base = base.upper().strip()
        now = now or time.time()
        entry = self._cache.get(base)
        snapshot: Optional[MarketSnapshot] = None
        if entry and not force and (now - entry.at) < self.cache_seconds:
            snapshot = entry.snapshot
            self.stats["cache_hits"] += 1
        if snapshot is None:
            timeframes = [self.cfg.entry_tf, *self.cfg.confirm_tfs, self.cfg.context_tf]
            try:
                snapshot = await self.provider.snapshot(
                    base, timeframes, limit=self.candles_limit
                )
            except Exception as exc:  # noqa: BLE001 — сеть не должна ронять бота
                self.stats["errors"] += 1
                log.warning("Не смог получить данные по %s: %s", base, exc)
                snapshot = MarketSnapshot(
                    base=base, symbol="", exchange=getattr(self.provider, "exchange_id", "?"),
                    errors=[f"ошибка запроса к бирже: {type(exc).__name__}"],
                )
        carry = self.carry_lookup(base) if self.carry_lookup else None
        signal = analyze(
            snapshot,
            profile=profile,
            cfg=self.cfg,
            now=now,
            carry_hint=carry.as_hint() if carry else None,
        )
        self.stats["analyses"] += 1
        self._cache[base] = _CacheEntry(signal=signal, snapshot=snapshot, at=now)

        # журнал точности: фиксируем выданный сигнал и двигаем открытые записи
        if signal.price:
            self.history.update_price(base, signal.price, now)
        self.history.record(signal)
        return signal

    async def scan(
        self,
        profile: RiskProfile,
        *,
        bases: Optional[Sequence[str]] = None,
        limit: int = 5,
        now: Optional[float] = None,
    ) -> tuple[list[DirectionalSignal], int, int]:
        """
        Скан рынка: берёт вселенную монет (топ по обороту с биржи, никаких
        зашитых списков), считает всех и возвращает лучших.

        Возвращает (лучшие сигналы, сколько просканировано, сколько отсеяно
        по качеству данных).
        """
        now = now or time.time()
        screened_out = 0
        ranks: dict[str, int] = {}
        if bases is None:
            # ЭТАП A — быстрый скрининг всей биржи (оборот, движение, размах).
            try:
                rows = await self.provider.screen(
                    min_quote_volume=profile.min_quote_volume_24h,
                    limit=self.universe_size * 4,
                )
            except NotImplementedError:
                rows = []
            except Exception as exc:  # noqa: BLE001
                log.warning("Скрининг не удался: %s", exc)
                rows = []
            if rows:
                total_pairs = len(rows)
                keep = rows[: self.universe_size]
                screened_out = max(0, total_pairs - len(keep))
                bases = [r.base for r in keep]
                ranks = {r.base: i + 1 for i, r in enumerate(keep)}
            else:
                try:
                    universe = await self.provider.universe(self.universe_size)
                    bases = [b for b, _ in universe]
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не смог получить список монет: %s", exc)
                    bases = []
        results: list[DirectionalSignal] = []

        async def worker(base: str) -> None:
            async with self._sem:
                try:
                    sig = await self.analyze_base(base, profile, now=now)
                    sig.screen_rank = ranks.get(base)
                    results.append(sig)
                except Exception as exc:  # noqa: BLE001
                    self.stats["errors"] += 1
                    log.warning("Скан %s упал: %s", base, exc)

        await asyncio.gather(*(worker(b) for b in bases))
        self.stats["scans"] += 1

        low_quality = sum(
            1 for s in results if s.data_confidence < profile.min_data_confidence
        )
        actionable = [s for s in results if s.actionable]
        actionable.sort(
            key=lambda s: (s.signal_confidence, s.plan.rr if s.plan else 0.0),
            reverse=True,
        )
        return actionable[:limit], len(results), low_quality

    # --------------------------------------------------------------- watchlist
    def watchlist(self, chat_id: str) -> set[str]:
        return self._watchlist.setdefault(str(chat_id), set())

    def watch_add(self, chat_id: str, base: str) -> set[str]:
        self.watchlist(chat_id).add(base.upper())
        return self.watchlist(chat_id)

    def watch_remove(self, chat_id: str, base: str) -> set[str]:
        self.watchlist(chat_id).discard(base.upper())
        return self.watchlist(chat_id)

    async def check_alerts(
        self, *, cooldown_minutes: float = 60.0, now: Optional[float] = None
    ) -> list[tuple[str, DirectionalSignal]]:
        """
        Проходит по watchlist всех чатов и возвращает [(chat_id, сигнал)] для
        монет, где ПРЯМО СЕЙЧАС есть проходной сигнал (по профилю чата).
        Антиспам: не чаще раза в `cooldown_minutes` на пару (чат, монета).
        """
        now = now or time.time()
        out: list[tuple[str, DirectionalSignal]] = []
        for chat_id, bases in self._watchlist.items():
            profile = self.profile_for(chat_id)
            for base in sorted(bases):
                key = (chat_id, base)
                if now - self._alert_sent.get(key, 0.0) < cooldown_minutes * 60.0:
                    continue
                try:
                    sig = await self.analyze_base(base, profile, now=now)
                except Exception:  # noqa: BLE001
                    continue
                if sig.actionable:
                    self._alert_sent[key] = now
                    self._alerts_log.append(
                        f"{time.strftime('%H:%M', time.gmtime(now))} {base} "
                        f"{sig.direction.upper()} {sig.signal_confidence:.0f}%"
                    )
                    out.append((chat_id, sig))
        return out

    # ------------------------------------------------------- обработчики команд
    async def cmd_analyze(self, chat_id: str, args: str) -> tuple[str, dict]:
        base = (args or "").split()[0].upper() if args else ""
        if not base:
            return (
                "🔎 Напишите тикер монеты: <code>/an BTC</code>, "
                "<code>/an sol</code>, <code>/an PEPE</code> — любая монета с биржи.\n\n"
                "Или нажмите «🏆 Топ сигналов», чтобы бот сам нашёл лучшее.",
                view.directional_menu_keyboard(),
            )
        profile = self.profile_for(chat_id)
        sig = await self.analyze_base(base, profile, force=True)
        return (
            view.format_signal_card(sig),
            view.signal_keyboard(base, in_watchlist=base in self.watchlist(chat_id)),
        )

    async def cmd_why(self, chat_id: str, args: str) -> tuple[str, dict]:
        base = (args or "").split()[0].upper() if args else ""
        if not base:
            return ("Укажите монету: <code>/why BTC</code>", view.directional_menu_keyboard())
        entry = self._cache.get(base)
        if entry is None:
            sig = await self.analyze_base(base, self.profile_for(chat_id))
        else:
            sig = entry.signal
        return (
            view.format_why_card(sig),
            view.signal_keyboard(base, in_watchlist=base in self.watchlist(chat_id)),
        )

    async def cmd_learn(self, chat_id: str, args: str) -> tuple[str, dict]:
        """Подробное объяснение сетапа для новичка (по кнопке под карточкой)."""
        base = (args or "").split()[0].upper() if args else ""
        if not base:
            return ("Укажите монету: <code>/learn BTC</code>", view.directional_menu_keyboard())
        entry = self._cache.get(base)
        sig = entry.signal if entry else await self.analyze_base(base, self.profile_for(chat_id))
        return (
            view.format_beginner_card(sig),
            view.signal_keyboard(base, in_watchlist=base in self.watchlist(chat_id)),
        )

    async def cmd_scan(self, chat_id: str, args: str) -> tuple[str, dict]:
        profile = self.profile_for(chat_id)
        started = time.time()
        limit = 5
        if args.strip().isdigit():
            limit = max(1, min(10, int(args.strip())))
        signals, scanned, low = await self.scan(profile, limit=limit)
        html = view.format_top_signals(
            signals, profile=profile, scanned=scanned, skipped=low,
            elapsed=time.time() - started,
        )
        return html, view.directional_menu_keyboard()

    async def cmd_profile(self, chat_id: str, args: str) -> tuple[str, dict]:
        parts = [p for p in (args or "").split(":") if p]
        key = parts[0].strip().lower() if parts else ""
        base = parts[1].strip().upper() if len(parts) > 1 else ""
        if key not in RISK_PROFILES:
            rows = "\n".join(f"• {p.describe()}" for p in RISK_PROFILES.values())
            return (
                "⚙️ <b>Риск-профиль</b>\n\n"
                f"Сейчас: <b>{self.profile_for(chat_id).title}</b>\n\n{rows}\n\n"
                "Профиль меняет только СТРОГОСТЬ фильтров и размер риска на сделку.",
                view.profiles_keyboard(),
            )
        profile = self.set_profile(chat_id, key)
        if base:
            sig = await self.analyze_base(base, profile, force=True)
            return (
                view.format_signal_card(sig),
                view.signal_keyboard(base, in_watchlist=base in self.watchlist(chat_id)),
            )
        return (
            f"✅ Профиль установлен: <b>{profile.title}</b>\n{profile.describe()}",
            view.directional_menu_keyboard(),
        )

    async def cmd_watch(self, chat_id: str, args: str) -> tuple[str, dict]:
        base = (args or "").split()[0].upper() if args else ""
        if base:
            self.watch_add(chat_id, base)
        items = sorted(self.watchlist(chat_id))
        return (
            view.format_watchlist_card(items, self._alerts_log),
            view.signal_keyboard(base, in_watchlist=True) if base
            else view.directional_menu_keyboard(),
        )

    async def cmd_unwatch(self, chat_id: str, args: str) -> tuple[str, dict]:
        base = (args or "").split()[0].upper() if args else ""
        if base:
            self.watch_remove(chat_id, base)
        items = sorted(self.watchlist(chat_id))
        return (
            view.format_watchlist_card(items, self._alerts_log),
            view.signal_keyboard(base, in_watchlist=False) if base
            else view.directional_menu_keyboard(),
        )

    async def cmd_accuracy(self, chat_id: str, args: str) -> tuple[str, dict]:
        base = (args or "").split()[0].upper() if args else None
        stats = self.history.stats(base)
        recent = self.history.recent(10, base)
        return (
            view.format_accuracy_card(stats, recent, base),
            view.signal_keyboard(base, in_watchlist=base in self.watchlist(chat_id))
            if base else view.directional_menu_keyboard(),
        )

    PAGE_SIZE = 12

    async def _universe_cached(self, limit: int = 240) -> list[tuple[str, float]]:
        now = time.time()
        cached = getattr(self, "_universe_cache", None)
        if cached and now - cached[0] < 300:
            return cached[1]
        try:
            universe = await self.provider.universe(limit)
        except Exception:  # noqa: BLE001
            universe = []
        self._universe_cache = (now, universe)
        return universe

    async def cmd_find(self, chat_id: str, args: str) -> tuple[str, dict]:
        """
        Полностью кнопочный поиск монеты: страницы по обороту + фильтр по букве.
        Печатать тикер по-прежнему можно, но не обязательно.

        Аргументы callback: "" | "p:<номер>" | "az:<буква>" | "az:<буква>:p:<n>"
        """
        arg = (args or "").strip()
        page, letter = 0, None
        if arg and not arg.lower().startswith(("p:", "az:")):
            return await self.cmd_analyze(chat_id, arg.upper())
        parts = arg.split(":") if arg else []
        i = 0
        while i < len(parts):
            if parts[i] == "az" and i + 1 < len(parts):
                letter = parts[i + 1].upper()
                i += 2
            elif parts[i] == "p" and i + 1 < len(parts):
                try:
                    page = max(0, int(parts[i + 1]))
                except ValueError:
                    page = 0
                i += 2
            else:
                i += 1

        universe = await self._universe_cached()
        coins = [b for b, _ in universe]
        if letter:
            coins = [c for c in coins if c.startswith(letter)]
        total_pages = max(1, (len(coins) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = min(page, total_pages - 1)
        chunk = coins[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]

        rows: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for base in chunk:
            row.append({"text": base, "callback_data": f"an:{base}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        base_cb = f"find:az:{letter}" if letter else "find"
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "◀️ Назад", "callback_data": f"{base_cb}:p:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": "find"})
        if page < total_pages - 1:
            nav.append({"text": "Вперёд ▶️", "callback_data": f"{base_cb}:p:{page + 1}"})
        if nav:
            rows.append(nav)

        # алфавит: только те буквы/цифры, с которых реально начинаются монеты
        starts = sorted({c[0] for c in [b for b, _ in universe]})
        letters_row: list[dict[str, str]] = []
        for ch in starts:
            letters_row.append({"text": ch, "callback_data": f"find:az:{ch}"})
            if len(letters_row) == 9:
                rows.append(letters_row)
                letters_row = []
        if letters_row:
            rows.append(letters_row)
        if letter:
            rows.append([{"text": "🔁 Все монеты", "callback_data": "find"}])
        rows.append([
            {"text": "🏆 Топ сигналов", "callback_data": "scan"},
            {"text": "🧭 Меню", "callback_data": "menu"},
        ])

        head = (f"🔎 <b>Поиск монеты</b> — буква «{letter}»" if letter
                else "🔎 <b>Поиск монеты</b> — по обороту за 24ч")
        text = (
            f"{head}\n\n"
            f"Нажмите на тикер — получите полный разбор. Всего доступно "
            f"<b>{len(coins)}</b> монет с биржи (список не захардкожен, "
            f"берётся с биржи прямо сейчас).\n"
            f"Можно и просто написать тикер сообщением."
        )
        return text, {"inline_keyboard": rows}

    async def cmd_setups(self, chat_id: str, args: str) -> tuple[str, dict]:
        """Справка: какие паттерны бот ищет и что проверено, что отвергнуто."""
        from setups import REJECTED_PATTERNS, SETUP_STATS, describe_setup
        lines = ["🧩 <b>Что именно ищет бот</b>", ""]
        for key in ("panic_reversal", "squeeze_breakdown"):
            lines.append(describe_setup(key))
            a = SETUP_STATS[key]["spot_1h"]
            b = SETUP_STATS[key]["perp_4h"]
            lines.append(
                f"<i>Проверено: {a.sample} → {a.trades} сделок, win {a.win_rate:.0f}%, "
                f"PF {a.profit_factor:.2f}; {b.sample} → {b.trades} сделок, "
                f"win {b.win_rate:.0f}%, PF {b.profit_factor:.2f}.</i>"
            )
            lines.append("")
        lines.append("🚫 <b>Что проверено и НЕ работает</b> (поэтому бот этого не предлагает):")
        for name, why in REJECTED_PATTERNS:
            lines.append(f"• <b>{name}</b> — {why}")
        lines.append("")
        lines.append(view.DISCLAIMER)
        kb = {"inline_keyboard": [
            [{"text": "🏆 Топ сигналов", "callback_data": "scan"},
             {"text": "🔎 Найти монету", "callback_data": "find"}],
            [{"text": "🧭 Меню", "callback_data": "menu"}],
        ]}
        return "\n".join(lines), kb

    async def cmd_menu(self, chat_id: str, args: str) -> tuple[str, dict]:
        profile = self.profile_for(chat_id)
        stats = self.history.stats()
        wr = stats["winrate"]
        text = (
            "🧭 <b>Направленный анализ</b>\n\n"
            f"Профиль риска: <b>{profile.title}</b>\n"
            f"{profile.describe()}\n\n"
            f"Таймфреймы: вход {self.cfg.entry_tf}, подтверждение "
            f"{', '.join(self.cfg.confirm_tfs)}, контекст {self.cfg.context_tf}\n"
            f"Журнал сигналов: {stats['total']} шт."
            + (f", win-rate {wr:.0f}%" if wr is not None else ", win-rate пока не считается")
            + "\n\n"
            "Всё работает кнопками ниже — команды печатать не нужно.\n"
            "🏆 топ сигналов · 🔎 выбор монеты · 🧩 какие паттерны ищу · "
            "👁 watchlist · 📈 точность.\n\n"
            + view.BACKTEST_NOTE + "\n\n" + view.DISCLAIMER
        )
        return text, view.directional_menu_keyboard(profile.key)

    def handlers(self) -> dict[str, Any]:
        """Реестр команд/кнопок направленного модуля."""
        return {
            "an": self.cmd_analyze,
            "analyze": self.cmd_analyze,
            "why": self.cmd_why,
            "learn": self.cmd_learn,
            "explain": self.cmd_learn,
            "scan": self.cmd_scan,
            "signals_top": self.cmd_scan,
            "profile": self.cmd_profile,
            "risk": self.cmd_profile,
            "watch": self.cmd_watch,
            "unwatch": self.cmd_unwatch,
            "accuracy": self.cmd_accuracy,
            "find": self.cmd_find,
            "setups": self.cmd_setups,
            "how": self.cmd_setups,
            "menu": self.cmd_menu,
            "trade": self.cmd_menu,
        }
