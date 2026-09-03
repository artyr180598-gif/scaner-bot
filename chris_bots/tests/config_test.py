"""
Тесты конфигурации: автозагрузка `.env` и валидация TELEGRAM_TOKEN.

Регрессия на баг «settings invalid: TELEGRAM_TOKEN is required» —
`.env` создавался по README, но никем не читался.

Запуск: python -m chris_bots.tests.config_test
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Dict, List

from chris_bots.config.settings import (
    TOKEN_ENV_NAMES,
    Settings,
    get_settings,
    load_env,
    loaded_env_file,
    loaded_env_keys,
    parse_env_text,
    reset_settings_cache,
    token_env_name,
)

OK = "[OK]  "
FAIL = "[FAIL]"

_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"

# Ключи, которые тесты трогают — их нужно вернуть как было.
_TOUCHED = (
    "TELEGRAM_TOKEN",
    "BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TG_TOKEN",
    "BOT_API_TOKEN",
    "ENV_FILE",
    "DOTENV_PATH",
    "DRY_RUN",
    "LOG_LEVEL",
)


class _env_sandbox:
    """Сохраняет/восстанавливает os.environ и кеш настроек вокруг теста."""

    def __enter__(self) -> None:
        self._saved: Dict[str, str] = {k: os.environ[k] for k in _TOUCHED if k in os.environ}
        for key in _TOUCHED:
            os.environ.pop(key, None)
        reset_settings_cache()

    def __exit__(self, *exc: object) -> None:
        for key in _TOUCHED:
            os.environ.pop(key, None)
        os.environ.update(self._saved)
        # Убираем ключи, приехавшие из временного .env, и сбрасываем состояние.
        for key in loaded_env_keys():
            if key not in self._saved:
                os.environ.pop(key, None)
        load_env(force=True)
        reset_settings_cache()


def _write_env(tmp: str, body: str) -> str:
    path = Path(tmp) / ".env"
    path.write_text(body, encoding="utf-8")
    return str(path)


# ── Парсер .env ────────────────────────────────────────────────
def t_parse_env_text():
    text = "\n".join([
        "# комментарий",
        "",
        "TELEGRAM_TOKEN=123456789:ABCdef",
        'QUOTED="value with spaces"',
        "SINGLE='single quoted'",
        "export EXPORTED=yes",
        "INLINE=value # хвостовой комментарий",
        "HASH_IN_VALUE=1#2",
        "ESCAPED=\"a\\nb\"",
        "EMPTY=",
        "не строка пары",
    ])
    parsed = parse_env_text(text)
    assert parsed["TELEGRAM_TOKEN"] == "123456789:ABCdef", parsed
    assert parsed["QUOTED"] == "value with spaces", parsed
    assert parsed["SINGLE"] == "single quoted", parsed
    assert parsed["EXPORTED"] == "yes", parsed
    assert parsed["INLINE"] == "value", parsed
    assert parsed["HASH_IN_VALUE"] == "1#2", parsed
    assert parsed["ESCAPED"] == "a\nb", parsed
    assert parsed["EMPTY"] == "", parsed
    assert "не строка пары" not in parsed
    print(OK + "parse_env_text: кавычки, export, комментарии")


# ── Автозагрузка .env ──────────────────────────────────────────
def t_dotenv_file_is_loaded():
    with _env_sandbox():
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_env(tmp, f"TELEGRAM_TOKEN={_TOKEN}\nDRY_RUN=false\n")
            os.environ["ENV_FILE"] = path

            loaded = load_env(force=True)
            assert loaded == path, f"ожидали {path}, получили {loaded}"
            assert os.environ.get("TELEGRAM_TOKEN") == _TOKEN, "токен не попал в окружение"
            assert loaded_env_file() == path

            # Главный сценарий бага: get_settings() видит токен из файла.
            settings = get_settings()
            assert settings.telegram_token == _TOKEN, "get_settings не подхватил .env"
            assert settings.dry_run is False
            settings.validate()  # не должно бросать
            print(OK + ".env подхватывается get_settings() + validate()")


def t_real_env_beats_file():
    with _env_sandbox():
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_env(tmp, "TELEGRAM_TOKEN=111:from_file\n")
            os.environ["ENV_FILE"] = path
            os.environ["TELEGRAM_TOKEN"] = "222:from_real_env"

            load_env(force=True)
            settings = get_settings()
            assert settings.telegram_token == "222:from_real_env", settings.telegram_token
            print(OK + "переменные окружения важнее .env")


def t_dotenv_file_found_without_env_file_var():
    """Бот запускают из корня проекта — .env лежит рядом с cwd."""
    with _env_sandbox():
        with tempfile.TemporaryDirectory() as tmp:
            _write_env(tmp, f"TELEGRAM_TOKEN={_TOKEN}\n")
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                loaded = load_env(force=True)
                assert loaded == str(Path(tmp) / ".env"), loaded
                assert get_settings().telegram_token == _TOKEN
            finally:
                os.chdir(cwd)
            print(OK + ".env находится по cwd без ENV_FILE")


def t_fallback_parser_without_python_dotenv():
    """Даже без установленного python-dotenv файл читается своим парсером."""
    with _env_sandbox():
        saved = sys.modules.get("dotenv", "missing")
        sys.modules["dotenv"] = None  # type: ignore[assignment]  → ImportError на импорте
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = _write_env(tmp, f'TELEGRAM_TOKEN="{_TOKEN}"\n')
                os.environ["ENV_FILE"] = path
                assert load_env(force=True) == path
                settings = get_settings()
                assert settings.telegram_token == _TOKEN, settings.telegram_token
                settings.validate()
        finally:
            if saved == "missing":
                sys.modules.pop("dotenv", None)
            else:
                sys.modules["dotenv"] = saved  # type: ignore[assignment]
        print(OK + "запасной парсер работает без python-dotenv")


# ── Валидация токена ───────────────────────────────────────────
def t_missing_token_message_is_actionable():
    with _env_sandbox():
        settings = get_settings()
        assert settings.telegram_token == ""
        try:
            settings.validate()
        except ValueError as exc:
            msg = str(exc)
            assert "TELEGRAM_TOKEN" in msg, msg
            assert "BotFather" in msg, msg
            assert ".env" in msg, msg
            print(OK + f"понятная ошибка: {msg[:78]}…")
            return
        raise AssertionError("validate() не бросил ошибку при пустом токене")


def t_placeholder_token_rejected():
    with _env_sandbox():
        try:
            Settings(telegram_token="your_token_here").validate()
        except ValueError as exc:
            assert "TELEGRAM_TOKEN" in str(exc)
            print(OK + "заглушка your_token_here отклонена")
            return
        raise AssertionError("заглушка не отклонена")


def test_token_without_colon_rejected():
    with _env_sandbox():
        try:
            Settings(telegram_token="просто-какая-то-строка").validate()
        except ValueError as exc:
            assert "123456789:AAHdqTcv" in str(exc)
            print(OK + "токен без двоеточия отклонён")
            return
        raise AssertionError("неверный формат токена не отклонён")


def t_token_whitespace_cleaned():
    with _env_sandbox():
        os.environ["TELEGRAM_TOKEN"] = f'  "{_TOKEN}"\n'
        reset_settings_cache()
        settings = get_settings()
        assert settings.telegram_token == _TOKEN, repr(settings.telegram_token)
        settings.validate()
        print(OK + "пробелы и кавычки вокруг токена обрезаны")


def t_settings_constructed_directly_reads_dotenv():
    """Settings() без get_settings() тоже обязан увидеть .env."""
    from chris_bots.config import settings as settings_mod

    with _env_sandbox():
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_env(tmp, f"TELEGRAM_TOKEN={_TOKEN}\nLLM_API_KEY=sk-test\n")
            os.environ["ENV_FILE"] = path
            # Имитируем свежий процесс: load_env() ещё не выполнялся.
            settings_mod._env_loaded = False
            settings_mod._loaded_env_file = None
            settings_mod._loaded_keys = ()

            s = Settings()
            assert s.telegram_token == _TOKEN, repr(s.telegram_token)
            assert s.llm_api_key == "sk-test", repr(s.llm_api_key)
            assert settings_mod.loaded_env_file() == path
            s.validate()
            print(OK + "Settings() напрямую тоже подхватывает .env")


def t_token_alias_bot_token():
    """Railway/Heroku: токен лежит в BOT_TOKEN, а не в TELEGRAM_TOKEN."""
    with _env_sandbox():
        os.environ["BOT_TOKEN"] = _TOKEN
        reset_settings_cache()
        settings = get_settings()
        assert settings.telegram_token == _TOKEN, repr(settings.telegram_token)
        assert token_env_name() == "BOT_TOKEN", token_env_name()
        settings.validate()
        print(OK + "BOT_TOKEN принимается как токен")


def t_all_token_aliases_accepted():
    """Каждое имя из TOKEN_ENV_NAMES должно работать."""
    for name in TOKEN_ENV_NAMES:
        with _env_sandbox():
            os.environ[name] = _TOKEN
            reset_settings_cache()
            settings = get_settings()
            assert settings.telegram_token == _TOKEN, f"{name}: {settings.telegram_token!r}"
            assert token_env_name() == name, f"{name} -> {token_env_name()}"
            settings.validate()
    print(OK + f"все {len(TOKEN_ENV_NAMES)} имён принимаются: {', '.join(TOKEN_ENV_NAMES)}")


def t_telegram_token_wins_over_alias():
    """Приоритет: TELEGRAM_TOKEN важнее BOT_TOKEN."""
    with _env_sandbox():
        os.environ["TELEGRAM_TOKEN"] = "111:primary_token_value"
        os.environ["BOT_TOKEN"] = "222:alias_token_value"
        reset_settings_cache()
        settings = get_settings()
        assert settings.telegram_token == "111:primary_token_value", settings.telegram_token
        assert token_env_name() == "TELEGRAM_TOKEN"
        print(OK + "TELEGRAM_TOKEN имеет приоритет над алиасами")


def t_empty_alias_falls_through():
    """Пустой BOT_TOKEN не должен маскировать настоящий TELEGRAM_TOKEN."""
    with _env_sandbox():
        os.environ["BOT_TOKEN"] = "   "
        os.environ["TELEGRAM_TOKEN"] = _TOKEN
        reset_settings_cache()
        settings = get_settings()
        assert settings.telegram_token == _TOKEN, repr(settings.telegram_token)
        assert token_env_name() == "TELEGRAM_TOKEN"
        print(OK + "пустой алиас пропускается")


def t_missing_token_lists_present_vars():
    """Ошибка должна показывать, какие похожие переменные реально есть в окружении."""
    with _env_sandbox():
        os.environ["MY_CUSTOM_SECRET"] = "123:not_a_token_name_we_track"
        try:
            get_settings().validate()
        except ValueError as exc:
            msg = str(exc)
            for name in TOKEN_ENV_NAMES:
                assert name in msg, f"{name} не упомянут в ошибке: {msg}"
            assert "MY_CUSTOM_SECRET" in msg, f"переменная не показана: {msg}"
            # Значение секрета не должно попасть в лог.
            assert "not_a_token_name_we_track" not in msg, "значение секрета утекло в ошибку!"
            print(OK + "ошибка перечисляет ожидаемые имена и найденные переменные")
            return
        raise AssertionError("validate() не бросил ошибку")


def t_valid_settings_pass():
    with _env_sandbox():
        os.environ["TELEGRAM_TOKEN"] = _TOKEN
        reset_settings_cache()
        get_settings().validate()
        print(OK + "валидная конфигурация проходит validate()")


TESTS: List = [
    t_parse_env_text,
    t_dotenv_file_is_loaded,
    t_real_env_beats_file,
    t_dotenv_file_found_without_env_file_var,
    t_fallback_parser_without_python_dotenv,
    t_missing_token_message_is_actionable,
    t_placeholder_token_rejected,
    test_token_without_colon_rejected,
    t_token_whitespace_cleaned,
    t_settings_constructed_directly_reads_dotenv,
    t_token_alias_bot_token,
    t_all_token_aliases_accepted,
    t_telegram_token_wins_over_alias,
    t_empty_alias_falls_through,
    t_missing_token_lists_present_vars,
    t_valid_settings_pass,
]


def main() -> int:
    print("\n=== config ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {t.__name__}")
            traceback.print_exc()
    total = len(TESTS)
    print(f"--- config: {total - failed}/{total} passed ---")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
