"""
Настройки бота-советника.

Загружаются из переменных окружения. Валидируются при старте (Settings.validate).

Перед чтением ENV автоматически подхватывается файл `.env` (см. load_env).

Приоритет (сверху вниз):
    1. Реальные переменные окружения процесса.
    2. Файл из ENV_FILE / DOTENV_PATH, если задан явно.
    3. `.env.local` и `.env` в текущей директории, затем в родительских,
       затем рядом с корнем пакета.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

ENV_FILE_NAMES: Tuple[str, ...] = (".env.local", ".env")

_loaded_env_file: Optional[str] = None
_loaded_keys: Tuple[str, ...] = ()
_env_loaded = False

TOKEN_ENV_NAMES: Tuple[str, ...] = (
    "TELEGRAM_TOKEN",
    "BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TG_TOKEN",
    "BOT_API_TOKEN",
)

_token_env_used: Optional[str] = None


# ── Загрузка .env ─────────────────────────────────────────────
def _candidate_env_files() -> List[Path]:
    raw_candidates: List[Path] = []
    for var in ("ENV_FILE", "DOTENV_PATH"):
        explicit = os.getenv(var)
        if explicit and explicit.strip():
            raw_candidates.append(Path(explicit.strip()).expanduser())

    cwd = Path.cwd().resolve()
    roots: List[Path] = [cwd, *cwd.parents]
    pkg_root = Path(__file__).resolve().parents[2]
    if pkg_root not in roots:
        roots.extend([pkg_root, *pkg_root.parents])

    for base in roots:
        for name in ENV_FILE_NAMES:
            raw_candidates.append(base / name)

    seen = set()
    result: List[Path] = []
    for path in raw_candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _searched_env_files(limit: int = 4) -> str:
    paths = [str(p) for p in _candidate_env_files()[:limit]]
    return ", ".join(paths) + (", …" if len(_candidate_env_files()) > limit else "")


def load_env() -> None:
    """Загружает .env (лениво, один раз). Реальные ENV всегда важнее."""
    global _env_loaded, _loaded_env_file, _loaded_keys
    if _env_loaded:
        return
    _env_loaded = True

    path: Optional[Path] = None
    for candidate in _candidate_env_files():
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        return

    try:
        from dotenv import dotenv_values
        values = dotenv_values(str(path))
    except Exception:  # noqa: BLE001
        values = _parse_env_manual(path.read_text(encoding="utf-8"))

    for key, val in values.items():
        if val is None:
            continue
        if key in os.environ:
            continue  # окружение процесса не перезаписываем
        os.environ[key] = val

    _loaded_env_file = str(path)
    _loaded_keys = tuple(k for k, v in values.items() if v is not None)


def _parse_env_manual(text: str) -> dict:
    """Крошечный парсер .env, если python-dotenv недоступен."""
    result = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def loaded_env_file() -> Optional[str]:
    return _loaded_env_file


# ── Токен ─────────────────────────────────────────────────────
def token_env_name() -> Optional[str]:
    return _token_env_used


def _tokenish_env_names() -> List[str]:
    markers = ("TOKEN", "TELEGRAM", "BOT", "TG_", "SECRET", "API_KEY")
    return sorted(k for k in os.environ if any(m in k.upper() for m in markers))


_TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{25,}$")


def _autodetect_token() -> Tuple[Optional[str], str]:
    for name in sorted(os.environ):
        if name in TOKEN_ENV_NAMES:
            continue
        candidate = (os.getenv(name) or "").strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in ("'", '"'):
            candidate = candidate[1:-1].strip()
        if _TELEGRAM_TOKEN_RE.match(candidate):
            return name, candidate
    return None, ""


def _clean_token() -> Tuple[str, Optional[str]]:
    global _token_env_used
    for name in TOKEN_ENV_NAMES:
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            continue
        _token_env_used = name
        token = raw.strip()
        warning: Optional[str] = None
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1].strip()
            warning = f"{name} был в кавычках — кавычки убраны"
        elif token != raw:
            warning = f"в {name} были лишние пробелы/переводы строк — обрезаны"
        return token, warning

    detected, token = _autodetect_token()
    _token_env_used = detected
    if detected is None:
        return "", None
    return token, (
        f"TELEGRAM_TOKEN не задан, но токен найден в переменной {detected} "
        "(опознан по формату). Лучше переименовать её в TELEGRAM_TOKEN."
    )


def _token_hint() -> str:
    expected = ", ".join(TOKEN_ENV_NAMES)
    if _loaded_env_file:
        source = f"загружен .env: {_loaded_env_file}"
    else:
        source = "файл .env не найден — беру только переменные окружения процесса"
    found = _tokenish_env_names()
    seen = ("в окружении процесса есть похожие переменные: " + ", ".join(found)) if found else (
        "в окружении процесса нет ни одной переменной, похожей на токен")
    return (
        f"ожидается одна из переменных: {expected}. Сейчас {seen} ({source}). "
        "Токен выдаёт @BotFather. Если на хостинге переменная называется иначе — "
        "переименуйте её в TELEGRAM_TOKEN."
    )


# ── Форматтеры ENV ────────────────────────────────────────────
def _list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "да")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except ValueError:
        return default


def _env_default(name: str) -> str:
    load_env()
    return os.getenv(name, "").strip()


def _token_default() -> str:
    load_env()
    token, warning = _clean_token()
    if warning:
        log.warning("%s", warning)
    return token


# ── Модель настроек ───────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    """Конфигурация бота-советника."""

    # Telegram
    telegram_token: str = field(default_factory=_token_default)
    admin_chat_ids: List[int] = field(default_factory=list)
    allowed_chat_ids: List[int] = field(default_factory=list)
    dry_run: bool = True

    # Биржи и данные
    exchanges: List[str] = field(default_factory=lambda: ["binance", "bybit"])
    quote_currencies: List[str] = field(default_factory=lambda: ["USDT"])
    top_n_symbols: int = 100
    base_timeframe: str = "1h"
    analysis_timeframes: List[str] = field(default_factory=lambda: ["1h", "4h", "1d"])
    candles_limit: int = 300

    # Движок
    min_confidence: float = 60.0        # порог по умолчанию (более мягкий, чем у v1)
    min_volume_usd_24h: float = 3_000_000.0
    min_atr_pct: float = 0.3
    max_atr_pct: float = 12.0
    anti_chase_window_bars: int = 12
    anti_chase_max_pct: float = 8.0
    max_matches: int = 8               # сколько монет показывать после подбора

    # Веса групп факторов
    weights_trend: float = 1.0
    weights_momentum: float = 0.9
    weights_volume: float = 0.8
    weights_volatility: float = 0.6
    weights_structure: float = 0.7
    weights_patterns: float = 0.5

    # LLM
    llm_enabled: bool = False
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = field(default_factory=lambda: _env_default("LLM_API_KEY"))
    llm_max_tokens: int = 260

    # Прочее
    log_level: str = "INFO"
    data_dir: str = "data"
    signals_db: str = "signals.db"

    def validate(self) -> None:
        token = self.telegram_token
        if not token or token.strip().lower() in ("", "none", "null", "your_token"):
            raise ValueError(f"TELEGRAM_TOKEN is required — {_token_hint()}")
        if any(ch.isspace() for ch in token):
            raise ValueError(
                "TELEGRAM_TOKEN содержит пробел/перевод строки внутри значения — "
                f"скопирован не весь токен или лишние символы. {_token_hint()}")
        if ":" not in token:
            raise ValueError(
                "TELEGRAM_TOKEN похож на неверный: ожидается формат "
                f"123456789:AAHdqTcv... (id бота, двоеточие, ключ). {_token_hint()}")
        if not self.exchanges:
            raise ValueError("at least one exchange is required")
        if self.base_timeframe not in self.analysis_timeframes:
            raise ValueError(
                f"BASE_TIMEFRAME={self.base_timeframe} must be in "
                f"ANALYSIS_TIMEFRAMES={self.analysis_timeframes}")
        if not (0.0 <= self.min_confidence <= 100.0):
            raise ValueError("MIN_CONFIDENCE must be in [0, 100]")
        if self.candles_limit < 50:
            raise ValueError("CANDLES_LIMIT must be >= 50")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон настроек. Подхватывает .env и парсит ENV один раз."""
    load_env()
    token, warning = _clean_token()
    if warning:
        log.warning("%s", warning)
    return Settings(
        telegram_token=token,
        admin_chat_ids=[int(x) for x in _list("ADMIN_CHAT_IDS") if x.lstrip("-").isdigit()],
        allowed_chat_ids=[int(x) for x in _list("ALLOWED_CHAT_IDS") if x.lstrip("-").isdigit()],
        dry_run=_bool("DRY_RUN", True),
        exchanges=_list("EXCHANGES", ["binance", "bybit"]) or ["binance"],
        quote_currencies=_list("QUOTE_CURRENCIES", ["USDT"]) or ["USDT"],
        top_n_symbols=_int("TOP_N_SYMBOLS", 100),
        base_timeframe=os.getenv("BASE_TIMEFRAME", "1h"),
        analysis_timeframes=_list("ANALYSIS_TIMEFRAMES", ["1h", "4h", "1d"]) or ["1h", "4h", "1d"],
        candles_limit=_int("CANDLES_LIMIT", 300),
        min_confidence=_float("MIN_CONFIDENCE", 60.0),
        min_volume_usd_24h=_float("MIN_VOLUME_USD_24H", 3_000_000.0),
        min_atr_pct=_float("MIN_ATR_PCT", 0.3),
        max_atr_pct=_float("MAX_ATR_PCT", 12.0),
        anti_chase_window_bars=_int("ANTI_CHASE_WINDOW_BARS", 12),
        anti_chase_max_pct=_float("ANTI_CHASE_MAX_PCT", 8.0),
        max_matches=_int("MAX_MATCHES", 8),
        llm_enabled=_bool("LLM_ENABLED", False),
        llm_provider=os.getenv("LLM_PROVIDER", "openai"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 260),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        data_dir=os.getenv("DATA_DIR", "data"),
        signals_db=os.getenv("SIGNALS_DB", "signals.db"),
    )


def reset_settings_cache() -> None:
    get_settings.cache_clear()
