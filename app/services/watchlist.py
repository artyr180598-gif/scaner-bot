"""
app/services/watchlist.py — списки наблюдения и настройки пользователей.

Хранение — один JSON-файл на всех (бот однопользовательский/для небольшой
команды). Настройки чата включают риск-профиль, пороги и подписку на авто-пуш.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.domain.models import RiskProfile, Timeframe

log = logging.getLogger(__name__)


@dataclass(slots=True)
class UserSettings:
    """Настройки одного чата (всё, что крутится кнопками «⚙️ Настройки»)."""

    risk_profile: str = RiskProfile.MODERATE.value
    deposit_usd: float = 1000.0
    min_confidence: float = 6.0
    min_rr: float = 1.8
    signal_timeframe: str = Timeframe.H1.value
    auto_push: bool = True
    show_beginner_hints: bool = True
    language: str = "ru"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "risk_profile": self.risk_profile,
            "deposit_usd": self.deposit_usd,
            "min_confidence": self.min_confidence,
            "min_rr": self.min_rr,
            "signal_timeframe": self.signal_timeframe,
            "auto_push": self.auto_push,
            "show_beginner_hints": self.show_beginner_hints,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserSettings":
        allowed = {k: v for k, v in (data or {}).items() if k in cls.__slots__}
        try:
            return cls(**allowed)  # type: ignore[arg-type]
        except TypeError:
            return cls()


@dataclass(slots=True)
class WatchItem:
    symbol: str
    added_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    note: str = ""


class Store:
    """Персистентное хранилище настроек и списков наблюдения."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: Dict[str, Any] = {"settings": {}, "watchlists": {}}
        self._lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data.update(raw)
                self._data.setdefault("settings", {})
                self._data.setdefault("watchlists", {})
        except Exception as exc:  # noqa: BLE001
            log.warning("хранилище настроек повреждено (%s) — создаю новое", exc)

    async def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=str(self.path.parent),
                                         delete=False, suffix=".tmp",
                                         encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=1)
            tmp = Path(fh.name)
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------
    def settings(self, chat_id: int) -> UserSettings:
        return UserSettings.from_dict(self._data["settings"].get(str(chat_id), {}))

    async def save_settings(self, chat_id: int, settings: UserSettings) -> None:
        async with self._lock:
            self._data["settings"][str(chat_id)] = settings.to_dict()
            await self._flush()

    def subscribed_chats(self) -> List[int]:
        """Чаты с включённым авто-пушем сигналов."""
        out: List[int] = []
        for chat_id, raw in self._data["settings"].items():
            if UserSettings.from_dict(raw).auto_push:
                try:
                    out.append(int(chat_id))
                except (TypeError, ValueError):
                    continue
        return out

    # ------------------------------------------------------------------
    # Списки наблюдения
    # ------------------------------------------------------------------
    def watchlist(self, chat_id: int) -> List[str]:
        items = self._data["watchlists"].get(str(chat_id), [])
        return [i["symbol"] if isinstance(i, dict) else str(i) for i in items]

    async def watch_add(self, chat_id: int, symbol: str) -> bool:
        async with self._lock:
            items = self._data["watchlists"].setdefault(str(chat_id), [])
            symbols = [i["symbol"] if isinstance(i, dict) else i for i in items]
            if symbol in symbols:
                return False
            items.append({"symbol": symbol,
                          "added_at": datetime.now(timezone.utc).isoformat()})
            await self._flush()
            return True

    async def watch_remove(self, chat_id: int, symbol: str) -> bool:
        async with self._lock:
            items = self._data["watchlists"].get(str(chat_id), [])
            new = [i for i in items
                   if (i["symbol"] if isinstance(i, dict) else i) != symbol]
            if len(new) == len(items):
                return False
            self._data["watchlists"][str(chat_id)] = new
            await self._flush()
            return True

    async def watch_clear(self, chat_id: int) -> None:
        async with self._lock:
            self._data["watchlists"][str(chat_id)] = []
            await self._flush()
