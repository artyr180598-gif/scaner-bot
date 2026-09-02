"""
app/config/settings.py — конфигурация из переменных окружения (.env).

Никаких глобальных состояний: ``Settings.from_env()`` возвращает неизменяемый
объект, который прокидывается в сервисы. Это позволяет в тестах собирать
конфиг на лету без monkeypatch-магии.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from app.domain.models import RiskProfile, Timeframe
from app.utils.errors import ConfigError

_TRUE = {"1", "true", "yes", "y", "on", "да"}
_FALSE = {"0", "false", "no", "n", "off", "нет"}


# ---------------------------------------------------------------------------
# Хелперы чтения env
# ---------------------------------------------------------------------------

def _get(name: str, default: str = "") -> str:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip()
    return val if val != "" else default


def _bool(name: str, default: bool) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name}: ожидалось true/false, получено {raw!r}")


def _int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось целое число, получено {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось число, получено {raw!r}") from exc


def _list(name: str, default: Sequence[str]) -> List[str]:
    raw = _get(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def _ints(items: Sequence[str]) -> List[int]:
    """Список чисел из уже разбитой строки; мусор молча отбрасывается."""
    out: List[int] = []
    for item in items:
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if value:
            out.append(value)
    return out


def _tf_list(name: str, default: Sequence[str]) -> List[Timeframe]:
    return [Timeframe.parse(item) for item in _list(name, default)]


# ---------------------------------------------------------------------------
# Пресеты риск-профилей
# ---------------------------------------------------------------------------

RISK_PRESETS: Dict[RiskProfile, Dict[str, float]] = {
    RiskProfile.CONSERVATIVE: {
        "risk_per_trade_pct": 0.5,
        "max_leverage": 2.0,
        "min_confidence": 6.5,
        "min_rr": 1.8,
        "stop_atr_mult": 1.8,
        "tp_atr_mults": (1.5, 2.5, 4.0),
        "horizon_hours": 48,
    },
    RiskProfile.MODERATE: {
        "risk_per_trade_pct": 1.0,
        "max_leverage": 3.0,
        "min_confidence": 5.5,
        "min_rr": 1.6,
        "stop_atr_mult": 1.5,
        "tp_atr_mults": (1.5, 2.5, 4.0),
        "horizon_hours": 24,
    },
    RiskProfile.AGGRESSIVE: {
        "risk_per_trade_pct": 2.0,
        "max_leverage": 5.0,
        "min_confidence": 4.5,
        "min_rr": 1.4,
        "stop_atr_mult": 1.2,
        "tp_atr_mults": (1.2, 2.0, 3.5),
        "horizon_hours": 12,
    },
}


@dataclass(slots=True)
class Settings:
    """Все настройки бота. Значения по умолчанию подобраны для продакшена."""

    # --- Telegram ----------------------------------------------------------
    telegram_bot_token: str = ""
    admin_chat_ids: List[int] = field(default_factory=list)
    allowed_chat_ids: List[int] = field(default_factory=list)  # пусто = доступ всем
    telegram_parse_mode: str = "HTML"

    # --- Биржи -------------------------------------------------------------
    exchanges: List[str] = field(default_factory=lambda: ["binance", "bybit", "okx"])
    derivatives_exchanges: List[str] = field(default_factory=lambda: ["binance", "bybit"])
    quote: str = "USDT"
    market_type: str = "spot"           # spot | swap (какие пары берём во вселенную)
    request_timeout_ms: int = 15000
    rate_limit_concurrency: int = 6
    rate_limit_min_interval: float = 0.06

    # --- Вселенная ---------------------------------------------------------
    max_universe: int = 250             # сколько пар анализируем по обороту
    min_quote_volume_usd: float = 3_000_000
    exclude_symbols: List[str] = field(
        default_factory=lambda: ["USDC", "FDUSD", "TUSD", "DAI", "BUSD", "EUR", "USDP"])
    include_symbols: List[str] = field(default_factory=list)  # всегда в сканировании
    exclude_leveraged_tokens: bool = True   # UP/DOWN/BULL/BEAR

    # --- Таймфреймы и данные ----------------------------------------------
    base_timeframe: Timeframe = Timeframe.H1   # качаем с биржи
    # Базовый таймфрейм качается с биржи, старшие — ресемплятся из него,
    # дневной берётся отдельным запросом (нужна длинная история для EMA200).
    # Поэтому младше base_timeframe здесь ничего быть не должно.
    analysis_timeframes: List[Timeframe] = field(
        default_factory=lambda: [Timeframe.H1, Timeframe.H4, Timeframe.D1])
    signal_timeframe: Timeframe = Timeframe.H1
    bars_base: int = 720                # ~30 суток часовых свечей
    bars_daily: int = 360               # год дневных — для EMA200/структуры
    cache_ttl_seconds: int = 60
    max_staleness_seconds: int = 300
    min_bars_required: int = 120

    # --- Скрининг ----------------------------------------------------------
    prescreen_candidates: int = 35      # кого тащим на глубокий анализ
    deep_analysis_concurrency: int = 6
    min_compression_percentile: float = 55.0   # сила «сжатия пружины», 0..100
    max_run_zscore: float = 2.2         # анти-погоня: не берём уже улетевшие
    max_distance_high_pct: float = 6.0  # не берём стоящие у самого хая без отката
    volume_anomaly_z: float = 2.0

    # --- Скоринг и сигналы -------------------------------------------------
    risk_profile: RiskProfile = RiskProfile.MODERATE
    deposit_usd: float = 1000.0
    risk_per_trade_pct: float = 1.0
    max_leverage: float = 3.0
    min_confidence: float = 5.5
    min_rr: float = 1.6
    max_open_signals: int = 12
    wait_threshold: float = 0.12        # |score| ниже — сигнал WAIT
    anti_chase_atr: float = 2.0         # вход дальше N ATR от уровня = «погоня»
    require_structure_alignment: bool = True

    # --- Сканер ------------------------------------------------------------
    scan_interval_minutes: int = 30
    auto_push: bool = True
    push_min_confidence: float = 7.0
    top_n: int = 8
    watchlist_cooldown_minutes: int = 120
    startup_scan: bool = True

    # --- Внешние источники (опционально) -----------------------------------
    news_enabled: bool = False
    cryptocompare_api_key: str = ""
    news_max_items: int = 12
    social_enabled: bool = False

    # --- Служебное ---------------------------------------------------------
    log_level: str = "INFO"
    data_dir: Path = field(default_factory=lambda: Path("data"))
    dry_run: bool = False               # не слать в Telegram (для локальной отладки)

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Settings":
        """Сборка из окружения. Вызывается один раз на старте."""
        if env_file:
            try:  # python-dotenv — необязательная зависимость
                from dotenv import load_dotenv

                load_dotenv(env_file)
            except ImportError:  # pragma: no cover
                pass

        s = cls()
        s.telegram_bot_token = _get("TELEGRAM_BOT_TOKEN")
        s.admin_chat_ids = _ints(_list("ADMIN_CHAT_IDS", []))
        s.allowed_chat_ids = _ints(_list("ALLOWED_CHAT_IDS", []))
        s.telegram_parse_mode = _get("TELEGRAM_PARSE_MODE", s.telegram_parse_mode)

        s.exchanges = [e.lower() for e in _list("EXCHANGES", s.exchanges)]
        s.derivatives_exchanges = [
            e.lower() for e in _list("DERIVATIVES_EXCHANGES", s.derivatives_exchanges)]
        s.quote = _get("QUOTE", s.quote).upper()
        s.market_type = _get("MARKET_TYPE", s.market_type).lower()
        s.request_timeout_ms = _int("REQUEST_TIMEOUT_MS", s.request_timeout_ms)
        s.rate_limit_concurrency = _int("RATE_LIMIT_CONCURRENCY", s.rate_limit_concurrency)
        s.rate_limit_min_interval = _float(
            "RATE_LIMIT_MIN_INTERVAL", s.rate_limit_min_interval)

        s.max_universe = _int("MAX_UNIVERSE", s.max_universe)
        s.min_quote_volume_usd = _float("MIN_QUOTE_VOLUME_USD", s.min_quote_volume_usd)
        s.exclude_symbols = [x.upper() for x in _list("EXCLUDE_SYMBOLS", s.exclude_symbols)]
        s.include_symbols = [x.upper() for x in _list("INCLUDE_SYMBOLS", s.include_symbols)]
        s.exclude_leveraged_tokens = _bool(
            "EXCLUDE_LEVERAGED_TOKENS", s.exclude_leveraged_tokens)

        s.base_timeframe = Timeframe.parse(_get("BASE_TIMEFRAME", s.base_timeframe.value))
        s.analysis_timeframes = _tf_list(
            "ANALYSIS_TIMEFRAMES", [t.value for t in s.analysis_timeframes])
        s.signal_timeframe = Timeframe.parse(
            _get("SIGNAL_TIMEFRAME", s.signal_timeframe.value))
        s.bars_base = _int("BARS_BASE", s.bars_base)
        s.bars_daily = _int("BARS_DAILY", s.bars_daily)
        s.cache_ttl_seconds = _int("CACHE_TTL_SECONDS", s.cache_ttl_seconds)
        s.max_staleness_seconds = _int("MAX_STALENESS_SECONDS", s.max_staleness_seconds)
        s.min_bars_required = _int("MIN_BARS_REQUIRED", s.min_bars_required)

        s.prescreen_candidates = _int("PRESCREEN_CANDIDATES", s.prescreen_candidates)
        s.deep_analysis_concurrency = _int(
            "DEEP_ANALYSIS_CONCURRENCY", s.deep_analysis_concurrency)
        s.min_compression_percentile = _float(
            "MIN_COMPRESSION_PERCENTILE", s.min_compression_percentile)
        s.max_run_zscore = _float("MAX_RUN_ZSCORE", s.max_run_zscore)
        s.max_distance_high_pct = _float("MAX_DISTANCE_HIGH_PCT", s.max_distance_high_pct)
        s.volume_anomaly_z = _float("VOLUME_ANOMALY_Z", s.volume_anomaly_z)

        s.risk_profile = RiskProfile.parse(_get("RISK_PROFILE", s.risk_profile.value))
        preset = RISK_PRESETS[s.risk_profile]
        s.deposit_usd = _float("DEPOSIT_USD", s.deposit_usd)
        s.risk_per_trade_pct = _float("RISK_PER_TRADE_PCT", preset["risk_per_trade_pct"])
        s.max_leverage = _float("MAX_LEVERAGE", preset["max_leverage"])
        s.min_confidence = _float("MIN_CONFIDENCE", preset["min_confidence"])
        s.min_rr = _float("MIN_RR", preset["min_rr"])
        s.max_open_signals = _int("MAX_OPEN_SIGNALS", s.max_open_signals)
        s.wait_threshold = _float("WAIT_THRESHOLD", s.wait_threshold)
        s.anti_chase_atr = _float("ANTI_CHASE_ATR", s.anti_chase_atr)
        s.require_structure_alignment = _bool(
            "REQUIRE_STRUCTURE_ALIGNMENT", s.require_structure_alignment)

        s.scan_interval_minutes = _int("SCAN_INTERVAL_MIN", s.scan_interval_minutes)
        s.auto_push = _bool("AUTO_PUSH", s.auto_push)
        s.push_min_confidence = _float("PUSH_MIN_CONFIDENCE", s.push_min_confidence)
        s.top_n = _int("TOP_N", s.top_n)
        s.watchlist_cooldown_minutes = _int(
            "WATCHLIST_COOLDOWN_MIN", s.watchlist_cooldown_minutes)
        s.startup_scan = _bool("STARTUP_SCAN", s.startup_scan)

        s.news_enabled = _bool("NEWS_ENABLED", s.news_enabled)
        s.cryptocompare_api_key = _get("CRYPTOCOMPARE_API_KEY")
        s.news_max_items = _int("NEWS_MAX_ITEMS", s.news_max_items)
        s.social_enabled = _bool("SOCIAL_ENABLED", s.social_enabled)

        s.log_level = _get("LOG_LEVEL", s.log_level).upper()
        s.data_dir = Path(_get("DATA_DIR", str(s.data_dir)))
        s.dry_run = _bool("DRY_RUN", s.dry_run)

        s.validate()
        return s

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Быстрая проверка на «самострел» в конфигурации."""
        problems: List[str] = []
        if self.bars_base < self.min_bars_required:
            problems.append(
                f"BARS_BASE={self.bars_base} меньше MIN_BARS_REQUIRED={self.min_bars_required}")
        if self.base_timeframe.minutes > min(t.minutes for t in self.analysis_timeframes):
            problems.append(
                "BASE_TIMEFRAME должен быть младшим из ANALYSIS_TIMEFRAMES "
                f"(сейчас {self.base_timeframe.value})")
        if self.signal_timeframe not in self.analysis_timeframes:
            problems.append(
                f"SIGNAL_TIMEFRAME={self.signal_timeframe.value} отсутствует в ANALYSIS_TIMEFRAMES")
        if not 0 < self.risk_per_trade_pct <= 10:
            problems.append("RISK_PER_TRADE_PCT должен быть в (0, 10]")
        if self.min_rr < 1.0:
            problems.append("MIN_RR < 1 — математически невыгодно")
        if self.max_leverage < 1:
            problems.append("MAX_LEVERAGE должен быть >= 1")
        if self.min_confidence < 0 or self.min_confidence > 10:
            problems.append("MIN_CONFIDENCE должен быть в [0, 10]")
        if self.scan_interval_minutes < 5:
            problems.append("SCAN_INTERVAL_MIN < 5 — упрёмся в rate limit бирж")
        if problems:
            raise ConfigError("Конфигурация некорректна:\n  - " + "\n  - ".join(problems))

    # ------------------------------------------------------------------
    def preset(self) -> Dict[str, float]:
        """Параметры риск-профиля, используемые планировщиком сделки."""
        return dict(RISK_PRESETS[self.risk_profile])

    def describe(self) -> str:
        return (
            f"биржи={','.join(self.exchanges)} | котирующая={self.quote} | "
            f"вселенная<={self.max_universe} (>{self.min_quote_volume_usd:,.0f}$) | "
            f"ТФ: база={self.base_timeframe.value}, анализ="
            f"{','.join(t.value for t in self.analysis_timeframes)}, "
            f"сигнал={self.signal_timeframe.value} | "
            f"риск={self.risk_profile.value} ({self.risk_per_trade_pct}%/сделку, "
            f"мин. уверенность {self.min_confidence}, мин. R:R {self.min_rr}) | "
            f"скан каждые {self.scan_interval_minutes} мин, авто-пуш={self.auto_push}"
        )

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if isinstance(val, Timeframe):
                val = val.value
            elif isinstance(val, RiskProfile):
                val = val.value
            elif isinstance(val, Path):
                val = str(val)
            out[f.name] = val
        return out
