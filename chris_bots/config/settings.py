"""
Настройки приложения.

Загружаются из переменных окружения. Валидируются при старте (см. Settings.validate).

Перед чтением ENV автоматически подхватывается файл `.env` (см. load_env):
это то, чего раньше не хватало — README просил сделать `cp .env.example .env`,
но файл никто не читал, и бот падал с «TELEGRAM_TOKEN is required».

Порядок приоритета (сверху вниз):
    1. Реальные переменные окружения процесса (никогда не перезаписываются).
    2. Файл из ENV_FILE / DOTENV_PATH, если задан явно.
    3. `.env.local` и `.env` в текущей директории, затем в родительских,
       затем рядом с корнем пакета.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Имена файлов в порядке убывания приоритета внутри одной директории.
ENV_FILE_NAMES: Tuple[str, ...] = (".env.local", ".env")

# Какой файл фактически загружен (для логов и понятных ошибок).
_loaded_env_file: Optional[str] = None
_loaded_keys: Tuple[str, ...] = ()
_env_loaded = False


def _candidate_env_files() -> List[Path]:
    """Все кандидаты `.env` в порядке приоритета (без дублей)."""
    raw_candidates: List[Path] = []

    for var in ("ENV_FILE", "DOTENV_PATH"):
        explicit = os.getenv(var)
        if explicit and explicit.strip():
            raw_candidates.append(Path(explicit.strip()).expanduser())

    # От cwd вверх до корня файловой системы.
    cwd = Path.cwd().resolve()
    roots: List[Path] = [cwd, *cwd.parents]

    # Корень репозитория (chris_bots/config/settings.py → parents[2]).
    # Нужно, когда бот запускают не из корня проекта.
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
    """Короткая сводка «где искали» — для текста ошибки."""
    paths = [str(p) for p in _candidate_env_files()[:limit]]
    return ", ".join(paths) + (", …" if len(_candidate_env_files()) > limit else "")


def _unescape_double_quoted(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def parse_env_text(text: str) -> Dict[str, str]:
    """
    Минимальный парсер `.env` — запасной вариант, если python-dotenv не установлен.

    Поддерживает: `KEY=value`, `export KEY=value`, кавычки ('...' / "..."),
    комментарии (`#`) и пустые строки.
    """
    result: Dict[str, str] = {}
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
        if not key or any(ch.isspace() for ch in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = _unescape_double_quoted(value)
        else:
            # Инлайн-комментарий только после пробела: VALUE # comment
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        result[key] = value
    return result


def _apply_vars(values: Dict[str, str]) -> List[str]:
    """Пишет значения в os.environ, не трогая уже заданные переменные."""
    applied: List[str] = []
    for key, value in values.items():
        if key in os.environ:
            continue  # Реальное окружение важнее файла.
        os.environ[key] = value
        applied.append(key)
    return applied


def load_env(force: bool = False) -> Optional[str]:
    """
    Загружает первый найденный `.env`. Идемпотентна.

    Возвращает путь к загруженному файлу или None, если файла нет.
    """
    global _loaded_env_file, _loaded_keys, _env_loaded

    if _env_loaded and not force:
        return _loaded_env_file
    _env_loaded = True

    try:
        from dotenv import dotenv_values  # type: ignore[import-not-found]
    except ImportError:  # python-dotenv не установлен — используем свой парсер.
        dotenv_values = None

    for path in _candidate_env_files():
        try:
            if not path.is_file():
                continue
            if dotenv_values is not None:
                values = {
                    k: v for k, v in dotenv_values(str(path)).items() if v is not None
                }
            else:
                values = parse_env_text(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            log.warning("cannot read env file %s: %s", path, exc)
            continue

        applied = _apply_vars(values)
        if applied:
            _loaded_env_file = str(path)
            _loaded_keys = tuple(sorted(applied))
            return _loaded_env_file
        # Файл найден, но все ключи уже заданы в окружении — всё равно считаем его источником.
        _loaded_env_file = str(path)
        return _loaded_env_file
    return None


def loaded_env_file() -> Optional[str]:
    """Путь к загруженному `.env` (None, если файла нет)."""
    return _loaded_env_file


def loaded_env_keys() -> Tuple[str, ...]:
    """Ключи, которые приехали из `.env` (не из окружения процесса)."""
    return _loaded_keys


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Значения-заглушки, которые часто оставляют в .env по привычке.
_TOKEN_PLACEHOLDERS = {
    "",
    "your_token_here",
    "your-telegram-bot-token",
    "<token>",
    "<telegram_token>",
    "telegram_token",
    "token",
    "xxx",
    "changeme",
    "change_me",
}


def _clean_token() -> Tuple[str, Optional[str]]:
    """
    Читает TELEGRAM_TOKEN и чистит его от типичных огрехов копипасты
    (пробелы/переводы строк по краям, обёртка в кавычки).

    Возвращает (токен, предупреждение).
    """
    raw = os.getenv("TELEGRAM_TOKEN", "")
    if raw is None:
        return "", None
    token = raw.strip()
    warning: Optional[str] = None
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        token = token[1:-1].strip()
        warning = "TELEGRAM_TOKEN был в кавычках — кавычки убраны"
    elif token != raw:
        warning = "в TELEGRAM_TOKEN были лишние пробелы/переводы строк — обрезаны"
    return token, warning


def _token_hint() -> str:
    """Подсказка для ошибки про отсутствующий токен."""
    if _loaded_env_file:
        source = f"загружен файл {_loaded_env_file}"
    else:
        source = f".env не найден (искали: {_searched_env_files()})"
    return (
        "получите токен у @BotFather и пропишите TELEGRAM_TOKEN=<токен> в .env "
        f"(cp .env.example .env) или экспортируйте в окружение. Сейчас: {source}."
    )


def _list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def _token_default() -> str:
    """Значение по умолчанию для telegram_token: .env подхватывается и тут."""
    load_env()
    token, warning = _clean_token()
    if warning:
        log.warning("%s", warning)
    return token


def _env_default(name: str) -> str:
    """Строковая переменная окружения с гарантированной загрузкой .env."""
    load_env()
    return os.getenv(name, "").strip()


@dataclass(frozen=True)
class Settings:
    """Конфигурация бота Крис."""

    # ── Telegram ───────────────────────────────────────────────
    telegram_token: str = field(default_factory=_token_default)
    admin_chat_ids: List[int] = field(default_factory=list)
    allowed_chat_ids: List[int] = field(default_factory=list)
    dry_run: bool = True

    # ── Биржи ──────────────────────────────────────────────────
    exchanges: List[str] = field(default_factory=lambda: ["binance", "bybit"])
    quote_currencies: List[str] = field(default_factory=lambda: ["USDT"])
    # Сколько топ-монет по объёму сканировать
    top_n_symbols: int = 100

    # ── Таймфреймы ─────────────────────────────────────────────
    # Базовый таймфрейм для сканера; старшие — для подтверждения.
    base_timeframe: str = "1h"
    analysis_timeframes: List[str] = field(default_factory=lambda: ["1h", "4h", "1d"])
    # Сколько свечей тянуть на каждом ТФ
    candles_limit: int = 300

    # ── Движок сигналов ────────────────────────────────────────
    min_confidence: float = 75.0  # жёсткий фильтр по ТЗ
    min_volume_usd_24h: float = 5_000_000.0  # анти-мусор
    min_atr_pct: float = 0.3
    max_atr_pct: float = 12.0
    # Анти-«погоня за движением» — актив уже дал импульс >N% за окно
    anti_chase_window_bars: int = 12
    anti_chase_max_pct: float = 8.0

    # ── Веса групп факторов (для confidence) ───────────────────
    # Схлопываем похожие сигналы в независимые группы (урок v4 BRAIN.md).
    weights_trend: float = 1.0
    weights_momentum: float = 0.9
    weights_volume: float = 0.8
    weights_volatility: float = 0.6
    weights_structure: float = 0.7
    weights_patterns: float = 0.5

    # ── LLM (опционально, для блока «Логика входа») ────────────
    llm_enabled: bool = False
    llm_provider: str = "openai"  # openai | anthropic | local
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = field(default_factory=lambda: _env_default("LLM_API_KEY"))
    llm_max_tokens: int = 220
    # Если LLM недоступна — используется детерминированный шаблон.

    # ── Логирование/хранение ────────────────────────────────────
    log_level: str = "INFO"
    data_dir: str = "data"
    signals_db: str = "signals.db"

    def validate(self) -> None:
        """Проверить инварианты. Бросает ValueError."""
        token = self.telegram_token
        if not token or token.strip().lower() in _TOKEN_PLACEHOLDERS:
            raise ValueError(f"TELEGRAM_TOKEN is required — {_token_hint()}")

        if any(ch.isspace() for ch in token):
            raise ValueError(
                "TELEGRAM_TOKEN содержит пробел/перевод строки внутри значения — "
                f"скопирован не весь токен или лишние символы. {_token_hint()}"
            )

        if ":" not in token:
            raise ValueError(
                "TELEGRAM_TOKEN похож на неверный: ожидается формат "
                f"123456789:AAHdqTcv... (id бота, двоеточие, ключ). {_token_hint()}"
            )

        if not self.exchanges:
            raise ValueError("at least one exchange is required")

        # Базовый ТФ должен присутствовать в списке анализа.
        if self.base_timeframe not in self.analysis_timeframes:
            raise ValueError(
                f"BASE_TIMEFRAME={self.base_timeframe} must be in "
                f"ANALYSIS_TIMEFRAMES={self.analysis_timeframes}"
            )

        if not (0.0 <= self.min_confidence <= 100.0):
            raise ValueError("MIN_CONFIDENCE must be in [0, 100]")

        if self.min_volume_usd_24h < 0:
            raise ValueError("MIN_VOLUME_USD_24H must be >= 0")

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
        min_confidence=_float("MIN_CONFIDENCE", 75.0),
        min_volume_usd_24h=_float("MIN_VOLUME_USD_24H", 5_000_000.0),
        min_atr_pct=_float("MIN_ATR_PCT", 0.3),
        max_atr_pct=_float("MAX_ATR_PCT", 12.0),
        anti_chase_window_bars=_int("ANTI_CHASE_WINDOW_BARS", 12),
        anti_chase_max_pct=_float("ANTI_CHASE_MAX_PCT", 8.0),
        llm_enabled=_bool("LLM_ENABLED", False),
        llm_provider=os.getenv("LLM_PROVIDER", "openai"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 220),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        data_dir=os.getenv("DATA_DIR", "data"),
        signals_db=os.getenv("SIGNALS_DB", "signals.db"),
    )


def reset_settings_cache() -> None:
    """Сбросить кеш настроек (нужно тестам и повторному перечитыванию ENV)."""
    get_settings.cache_clear()
