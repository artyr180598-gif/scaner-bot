"""
config.py — центральная точка конфигурации сканера.

Все настройки читаются из переменных окружения (Railway → Variables),
с безопасными значениями по умолчанию. Локально можно использовать .env
(подхватывается через python-dotenv в main.py).

Основные переменные (см. .env.example для полного списка):
    TELEGRAM_BOT_TOKEN   — токен бота из @BotFather
    CHAT_ID              — ID чата (можно несколько через запятую)
    MIN_SPREAD_PERCENT   — минимальный чистый спред, % (default: 2.0)
    COOLDOWN_MINUTES     — антиспам-пауза на пару, мин (default: 15)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

#: Биржи, которые сканер умеет обслуживать (id как в ccxt).
SUPPORTED_EXCHANGES: tuple[str, ...] = ("mexc", "bybit", "gate", "okx", "binance")

#: Дефолтный список ликвидных базовых активов — резерв на случай,
#: когда авто-подбор топа по объёму торгов недоступен (все reference-биржи молчат).
FALLBACK_BASES: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "TRX", "LINK",
    "DOT", "LTC", "BCH", "UNI", "ATOM", "XLM", "NEAR", "APT", "ARB", "OP",
    "FIL", "ICP", "ETC", "HBAR", "VET", "INJ", "SUI", "SEI", "TIA", "PEPE",
    "WIF", "SHIB", "RUNE", "AAVE", "MKR", "GRT", "IMX", "FTM", "ALGO", "EOS",
)

#: Стейблкоины и прочие «не-активы», которые не имеют смысла в spot/perp связке.
EXCLUDED_BASES: frozenset[str] = frozenset({
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "PAX", "SUSD",
    "AEUR", "EUR", "TRY", "BRL", "ARS", "COP", "JPY", "RUB", "UAH", "PLN",
    "WBTC", "WBETH", "STETH", "RETH", "WEETH",
})

#: Суффиксы леверидж-токенов (UP/DOWN, 3L/3S и т.п.) — у них нет перпов.
_LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")


def is_scannable_base(base: str) -> bool:
    """Пригоден ли базовый актив для spot/perp арбитражной связки."""
    if not base or len(base) > 16:
        return False
    if not base.isascii() or not base.isalnum():
        return False
    if base in EXCLUDED_BASES:
        return False
    if base.endswith(_LEVERAGED_SUFFIXES) and len(base) > 4:
        return False
    return True


# ---------------------------------------------------------------------------
# Парсеры переменных окружения (с валидацией и понятными ошибками)
# ---------------------------------------------------------------------------

def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_list_str(name: str) -> list[str]:
    value = os.getenv(name)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_float(
    name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Переменная {name} должна быть числом, получено: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"Переменная {name} должна быть >= {minimum}, получено: {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"Переменная {name} должна быть <= {maximum}, получено: {value}")
    return value


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    return int(_env_float(name, default, minimum=float(minimum)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y", "да")


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """Иммутабельный снимок конфигурации приложения."""

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: Optional[str] = None
    chat_ids: tuple[str, ...] = ()

    # --- Фильтр сигналов ----------------------------------------------------
    min_spread_percent: float = 2.0          # MIN_SPREAD_PERCENT (алиас MIN_SPREAD)
    cooldown_minutes: float = 15.0           # COOLDOWN_MINUTES — антиспам на пару
    spot_taker_fee_percent: float = 0.1      # SPOT_TAKER_FEE_PERCENT
    futures_taker_fee_percent: float = 0.05  # FUTURES_TAKER_FEE_PERCENT
    min_notional_usd: float = 0.0            # MIN_NOTIONAL_USD (0 = фильтр выключен)
    max_signals_per_scan: int = 5            # MAX_SIGNALS_PER_SCAN
    # По ТЗ спот и фьючерс должны быть на РАЗНЫХ биржах; True включает
    # дополнительно базисные связки «спот+перп на одной бирже».
    allow_same_exchange: bool = False         # ALLOW_SAME_EXCHANGE

    # --- Набор инструментов -------------------------------------------------
    symbols: tuple[str, ...] = ()            # SYMBOLS: BTC,ETH,... (пусто = авто)
    auto_discover_symbols: bool = True       # AUTO_DISCOVER_SYMBOLS
    top_symbols_limit: int = 30              # TOP_SYMBOLS

    # --- Биржи и сбор данных ------------------------------------------------
    exchanges: tuple[str, ...] = SUPPORTED_EXCHANGES  # EXCHANGES: mexc,bybit,...
    use_websocket: bool = True               # USE_WEBSOCKET (ccxt.pro watch_order_book)
    ws_fails_before_fallback: int = 10       # WS_FAILS_BEFORE_REST_FALLBACK
    order_book_depth: int = 5                # ORDER_BOOK_DEPTH (уровней в REST-запросе)
    rest_poll_interval_seconds: float = 3.0  # REST_POLL_INTERVAL_SECONDS (между кругами)
    rest_throttle_seconds: float = 0.1       # REST_THROTTLE_SECONDS (между запросами)
    book_max_age_seconds: float = 45.0       # BOOK_MAX_AGE_SECONDS (свежесть стакана)

    # --- Служебное ----------------------------------------------------------
    scan_interval_seconds: float = 5.0       # SCAN_INTERVAL_SECONDS
    heartbeat_minutes: float = 0.0           # HEARTBEAT_MINUTES (0 = выключен)
    market_refresh_minutes: float = 720.0    # MARKET_REFRESH_MINUTES
    status_log_minutes: float = 10.0         # STATUS_LOG_MINUTES
    restart_backoff_seconds: float = 30.0    # RESTART_BACKOFF_SECONDS
    log_level: str = "INFO"                  # LOG_LEVEL

    # --- Вычисляемое --------------------------------------------------------
    @property
    def total_fee_percent(self) -> float:
        """Суммарная комиссия (в %), вычитаемая из гросс-спреда."""
        return self.spot_taker_fee_percent + self.futures_taker_fee_percent

    @property
    def cooldown_seconds(self) -> float:
        return self.cooldown_minutes * 60.0

    # -----------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "Settings":
        """Собирает настройки из окружения; бросает ValueError при ошибках."""
        exchanges = tuple(
            dict.fromkeys(
                item.lower()
                for item in _env_list_str("EXCHANGES")
                or list(SUPPORTED_EXCHANGES)
            )
        )
        unknown = [e for e in exchanges if e not in SUPPORTED_EXCHANGES]
        if unknown:
            raise ValueError(
                f"EXCHANGES: неподдерживаемые биржи {unknown}. "
                f"Доступно: {list(SUPPORTED_EXCHANGES)}"
            )

        symbols = tuple(
            dict.fromkeys(
                item.upper()
                for item in _env_list_str("SYMBOLS")
            )
        )
        if symbols:
            bad = [s for s in symbols if not is_scannable_base(s)]
            if bad:
                raise ValueError(f"SYMBOLS: некорректные тикеры {bad}")

        # MIN_SPREAD_PERCENT — основное имя; MIN_SPREAD — совместимый алиас.
        min_spread = _env_float("MIN_SPREAD_PERCENT", -1.0, minimum=-100.0)
        if min_spread == -1.0:  # не задано
            min_spread = _env_float("MIN_SPREAD", 2.0, minimum=-100.0)

        log_level = (_env_str("LOG_LEVEL", "INFO") or "INFO").upper()

        return cls(
            telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
            chat_ids=tuple(_env_list_str("CHAT_ID")),
            min_spread_percent=min_spread,
            cooldown_minutes=_env_float("COOLDOWN_MINUTES", 15.0, minimum=0.0),
            spot_taker_fee_percent=_env_float("SPOT_TAKER_FEE_PERCENT", 0.1, minimum=0.0),
            futures_taker_fee_percent=_env_float("FUTURES_TAKER_FEE_PERCENT", 0.05, minimum=0.0),
            min_notional_usd=_env_float("MIN_NOTIONAL_USD", 0.0, minimum=0.0),
            max_signals_per_scan=_env_int("MAX_SIGNALS_PER_SCAN", 5, minimum=1),
            allow_same_exchange=_env_bool("ALLOW_SAME_EXCHANGE", False),
            symbols=symbols,
            auto_discover_symbols=_env_bool("AUTO_DISCOVER_SYMBOLS", True),
            top_symbols_limit=_env_int("TOP_SYMBOLS", 30, minimum=1),
            exchanges=exchanges,
            use_websocket=_env_bool("USE_WEBSOCKET", True),
            ws_fails_before_fallback=_env_int("WS_FAILS_BEFORE_REST_FALLBACK", 10, minimum=1),
            order_book_depth=_env_int("ORDER_BOOK_DEPTH", 5, minimum=1),
            rest_poll_interval_seconds=_env_float("REST_POLL_INTERVAL_SECONDS", 3.0, minimum=0.0),
            rest_throttle_seconds=_env_float("REST_THROTTLE_SECONDS", 0.1, minimum=0.0),
            book_max_age_seconds=_env_float("BOOK_MAX_AGE_SECONDS", 45.0, minimum=1.0),
            scan_interval_seconds=_env_float("SCAN_INTERVAL_SECONDS", 5.0, minimum=1.0),
            heartbeat_minutes=_env_float("HEARTBEAT_MINUTES", 0.0, minimum=0.0),
            market_refresh_minutes=_env_float("MARKET_REFRESH_MINUTES", 720.0, minimum=10.0),
            status_log_minutes=_env_float("STATUS_LOG_MINUTES", 10.0, minimum=0.0),
            restart_backoff_seconds=_env_float("RESTART_BACKOFF_SECONDS", 30.0, minimum=1.0),
            log_level=log_level,
        )

    # -----------------------------------------------------------------------
    def describe(self) -> str:
        """Человекочитаемое резюме конфигурации для стартового лога."""
        token_state = (
            "OK" if (self.telegram_bot_token and self.chat_ids)
            else "НЕТ → режим DRY-RUN (сигналы только в логи)"
        )
        fee_line = (
            f"{self.spot_taker_fee_percent:.2f}% spot + "
            f"{self.futures_taker_fee_percent:.2f}% futures = {self.total_fee_percent:.2f}%"
        )
        mode = "WebSocket + REST-fallback" if self.use_websocket else "только REST (polling)"
        sources = ", ".join(self.symbols) if self.symbols else (
            f"авто-подбор топ-{self.top_symbols_limit} по объёму торгов"
            if self.auto_discover_symbols
            else f"резервный список ({len(FALLBACK_BASES)} монет)"
        )
        return (
            "Конфигурация:\n"
            f"  Биржи:            {', '.join(e.upper() for e in self.exchanges)}\n"
            f"  Источник данных:  {mode}\n"
            f"  Инструменты:      {sources}\n"
            f"  Порог спреда:     {self.min_spread_percent:.2f}% (чистыми)\n"
            f"  Комиссии:         {fee_line}\n"
            f"  Кулдаун сигнала:  {self.cooldown_minutes:.0f} мин на пару\n"
            f"  Фильтр ликвидности: "
            f"{'выключен' if self.min_notional_usd <= 0 else f'>= ${self.min_notional_usd:,.0f}'}\n"
            f"  Одинаковая биржа (базис): {'разрешена' if self.allow_same_exchange else 'запрещена'}\n"
            f"  Telegram:         {token_state}"
        )
