"""
Регрессия на команду запуска.

Хостинг поднимает бота командой ``python -m chris_bots.main``. Пакет в репо
называется ``crypto_advisor``, поэтому раньше падало с
``ModuleNotFoundError: No module named 'chris_bots'``. Здесь проверяем, что
алиас ``chris_bots`` реально резолвится и доходит до настоящего старта бота.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN_ENV = ("TELEGRAM_TOKEN", "BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_TOKEN", "BOT_API_TOKEN")


def _clean_env(tmp_path) -> dict:
    env = dict(os.environ)
    for name in TOKEN_ENV:
        env.pop(name, None)
    # изолируемся от локального .env разработчика
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# no vars\n", encoding="utf-8")
    env["ENV_FILE"] = str(empty_env)
    env.pop("DOTENV_PATH", None)
    return env


def test_chris_bots_alias_points_to_crypto_advisor():
    import chris_bots.main as alias
    import crypto_advisor.main as canonical

    assert alias.main is canonical.main
    assert alias.run is canonical.run


def test_python_dash_m_chris_bots_main_resolves(tmp_path):
    """`python -m chris_bots.main` больше не падает на импорте модуля."""
    proc = subprocess.run(
        [sys.executable, "-m", "chris_bots.main"],
        cwd=str(REPO_ROOT),
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr

    assert "No module named 'chris_bots'" not in combined, combined
    assert "No module named 'crypto_advisor'" not in combined, combined
    # токена нет → бот доходит до валидации настроек и выходит с кодом 2
    assert proc.returncode == 2, combined
    assert "settings invalid" in combined, combined


def test_python_dash_m_chris_bots_selftest_runs(tmp_path):
    """Офлайн-проверка через алиас тоже запускается (run() — корутина)."""
    proc = subprocess.run(
        [sys.executable, "-m", "chris_bots.selftest"],
        cwd=str(REPO_ROOT),
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    combined = proc.stdout + proc.stderr
    assert "never awaited" not in combined, combined
    assert proc.returncode == 0, combined
    assert "=== Подбор монет ===" in combined, combined


def test_python_dash_m_crypto_advisor_main_resolves(tmp_path):
    """Каноническая команда тоже жива."""
    proc = subprocess.run(
        [sys.executable, "-m", "crypto_advisor.main"],
        cwd=str(REPO_ROOT),
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr
    assert "No module named" not in combined, combined
    assert proc.returncode == 2, combined
