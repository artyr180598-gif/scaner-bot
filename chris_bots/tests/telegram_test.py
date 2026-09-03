"""
Тесты Telegram-слоя: wiring хендлеров и кнопок «Быстрый/Глубокий скан».

Ключевой регрессионный сценарий: запуск `python -m chris_bots.main`.
При таком запуске main.py исполняется как __main__ и НЕ лежит в sys.modules
под именем `chris_bots.main`. Раньше хендлеры делали
`from ...main import app_state` — Python грузил main.py ВТОРОЙ раз, с пустым
app_state, `_get_engine()` падал с KeyError до edit_text/call.answer(), и
кнопки «⚡ Быстрый скан» / «🔎 Глубокий скан» молча ничего не делали.

Тест эмулирует ровно эту семантику импортов и прогоняет настоящий
Dispatcher с настоящими хендлерами (подменён только сетевой слой).
"""
from __future__ import annotations

import asyncio
import time
import types
from pathlib import Path

from chris_bots.tests.base import OK

PKG_ROOT = Path(__file__).resolve().parents[1]


def _exec_main_as_dunder_main() -> types.ModuleType:
    """
    Исполняет chris_bots/main.py как __main__ — как это делает
    `python -m chris_bots.main`:

    - модуль НЕ регистрируется в sys.modules как "chris_bots.main";
    - __package__ = "chris_bots" (относительные импорты работают);
    - entry-guard отключён, чтобы не стартовать polling.

    Возвращает namespace, в котором «живёт» продакшн-код бота.
    """
    import sys
    sys.modules.pop("chris_bots.main", None)  # эмулируем отсутствие модуля

    src = (PKG_ROOT / "main.py").read_text(encoding="utf-8")
    src = src.replace('if __name__ == "__main__":', "if False:  # test: без запуска бота")
    mod = types.ModuleType("__main__")
    mod.__file__ = str(PKG_ROOT / "main.py")
    mod.__package__ = "chris_bots"
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def test_handlers_do_not_import_main_module() -> None:
    """
    Хендлеры не должны импортировать chris_bots.main — иначе при
    `python -m chris_bots.main` получаем двойной импорт и пустой app_state.
    Зависимости берём только из chris_bots.runtime.
    Проверяем по AST — реальные import-ы, а не упоминания в комментариях.
    """
    import ast

    def _module_parts(path: Path) -> list:
        rel = path.relative_to(PKG_ROOT.parent).with_suffix("")
        parts = list(rel.parts)  # ["chris_bots", "telegram", ...]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return parts

    def _import_target(path: Path, node) -> str:
        """Полное имя модуля, который импортирует node (с учётом relative)."""
        parts = _module_parts(path)
        # пакет модуля: у mod.py это его директория, у __init__.py — он сам
        pkg = parts if path.name == "__init__.py" else parts[:-1]
        if isinstance(node, ast.ImportFrom) and node.level:
            pkg = pkg[: len(pkg) - (node.level - 1)]
            extra = node.module.split(".") if node.module else []
        else:
            extra = (node.module or "").split(".") if isinstance(node, ast.ImportFrom) else []
        return ".".join(pkg + [p for p in extra if p])

    def _imports_main(path: Path, tree) -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    t = a.name
                    if t == "chris_bots.main" or t.startswith("chris_bots.main."):
                        return True
            elif isinstance(node, ast.ImportFrom):
                t = _import_target(path, node)
                if t == "chris_bots.main" or t.startswith("chris_bots.main."):
                    return True
        return False

    bad = []
    for path in (PKG_ROOT / "telegram").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_main(path, tree):
            bad.append(str(path.relative_to(PKG_ROOT)))
    assert not bad, f"хендлеры импортируют main.py (ловушка двойного импорта): {bad}"

    import chris_bots.main as main_mod
    import chris_bots.runtime as rt
    assert main_mod.app_state is rt.app_state, "main.app_state должен быть тем же объектом, что runtime.app_state"
    print(OK + "хендлеры берут зависимости из chris_bots.runtime (не из main.py)")


def test_engine_visible_after_python_m_start() -> None:
    """`python -m chris_bots.main` + нажатие кнопки: движок должен находиться."""
    import chris_bots.runtime as rt

    mod = _exec_main_as_dunder_main()
    # То, что main() делает до старта polling:
    mod.set_dependencies(engine="FAKE-ENGINE", settings="FAKE-SETTINGS")

    from chris_bots.telegram.handlers.scan import _get_engine
    engine = _get_engine()  # раньше здесь был KeyError('engine')
    assert engine == "FAKE-ENGINE", f"хендлер видит чужой app_state: {engine!r}"

    rt.app_state.clear()  # не протекаем в другие тесты
    print(OK + "после `python -m` хендлер находит движок в runtime.app_state")


# ── End-to-end: настоящие хендлеры + настоящий Dispatcher ──────

def _make_ohlcv(n: int = 300, base: float = 100.0, seed: int = 7) -> list:
    import random
    rnd = random.Random(seed)
    rows, price = [], base
    now = int(time.time() * 1000)
    for i in range(n):
        o = price
        c = max(0.01, o * (1 + 0.004 + rnd.gauss(0, 0.012)))
        rows.append([
            now - (n - i) * 3600_000,
            o,
            max(o, c) * 1.001,
            min(o, c) * 0.999,
            c,
            rnd.uniform(5e5, 5e6),
        ])
        price = c
    return rows


class _FakeCcxtClient:
    async def fetch_time(self):
        return int(time.time() * 1000)

    async def fetch_tickers(self):
        return {
            f"COIN{i}/USDT": {
                "last": 10 + i,
                "quoteVolume": 50_000_000 + i * 1_000_000,
                "bid": (10 + i) * 0.999,
                "ask": (10 + i) * 1.001,
                "percentage": 1.2,
            }
            for i in range(20)
        }

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=300):
        idx = int(symbol.split("COIN")[1].split("/")[0]) if "COIN" in symbol else 0
        return _make_ohlcv(n=limit, base=10 + idx, seed=idx + 1)

    async def close(self):
        pass


def _build_fake_gateway():
    from chris_bots.data.exchange.gateway import ExchangeGateway
    gw = ExchangeGateway.__new__(ExchangeGateway)
    ExchangeGateway.__init__(gw, ["binance"], ["USDT"])
    gw._handles = {"binance": types.SimpleNamespace(name="binance", client=_FakeCcxtClient())}
    return gw


def test_scan_fast_button_end_to_end() -> None:
    """Кнопка «⚡ Быстрый скан» после старта через `python -m`: пользователь
    должен увидеть «⏳ Запускаю…» и итог скана (а не тишину)."""

    async def _run() -> None:
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.methods.base import TelegramMethod
        from aiogram.types import CallbackQuery, Chat, Message, Update, User

        from chris_bots.config.settings import Settings
        from chris_bots.runtime import set_dependencies
        from chris_bots.scanner import ScannerEngine
        from chris_bots.telegram.handlers import all_routers

        # 1) продакшн-порядок импортов: main.py уже исполнился как __main__
        mod = _exec_main_as_dunder_main()

        # 2) наполняем зависимости тем же кодом, что и main()
        gw = _build_fake_gateway()
        engine = ScannerEngine(settings=mod.get_settings(), gateway=gw)
        set_dependencies(engine=engine, gateway=gw, settings=mod.get_settings())

        # 3) настоящий Dispatcher + бот, у которого перехвачены API-вызовы
        calls: list[TelegramMethod] = []

        class _Session:
            async def close(self):
                pass

        class _RecordingBot(Bot):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.session = _Session()

            async def __call__(self, method: TelegramMethod, request_timeout=None):
                calls.append(method)
                name = type(method).__name__
                if name == "EditMessageText":
                    return True
                if name == "SendMessage":
                    return Message(message_id=len(calls), date=int(time.time()), chat=Chat(id=1, type="private"))
                if name == "AnswerCallbackQuery":
                    return True
                raise AssertionError(f"неожиданный вызов Telegram API: {name}")

        bot = _RecordingBot(
            token="123456789:AAHdqTcvabcdEFGHIJKLMNOPqrstuvwxyz12345",
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher()
        for r in all_routers():
            dp.include_router(r)

        async def press(callback_data: str) -> None:
            upd = Update(
                update_id=len(calls) + 1,
                callback_query=CallbackQuery(
                    id=str(len(calls)),
                    from_user=User(id=1, is_bot=False, first_name="T"),
                    chat_instance="ci",
                    data=callback_data,
                    message=Message(
                        message_id=42, date=int(time.time()),
                        chat=Chat(id=1, type="private"), text="menu",
                    ),
                ),
            )
            await dp.feed_update(bot, upd)

        # 4) открываем меню скана и жмём «Быстрый скан»
        await press("scan:market")
        menu_calls = len(calls)
        assert any(type(c).__name__ == "EditMessageText" for c in calls[:menu_calls]), "меню скана не открылось"

        await press("scan:fast")
        names = [type(c).__name__ for c in calls]
        texts = [getattr(c, "text", "") or "" for c in calls]
        rest = names[menu_calls:]

        assert "AnswerCallbackQuery" in rest, f"callback не отвечен (кнопка бы «крутилась»): {rest}"
        assert any("Запускаю" in t for t in texts), f"нет сообщения «⏳ Запускаю…»: {texts}"
        assert any("Скан рынка завершён" in t for t in texts), f"нет итогов скана: {texts}"

    asyncio.run(_run())
    print(OK + "кнопка «Быстрый скан» отвечает: «⏳ Запускаю…» → итог скана")


TESTS = [
    test_handlers_do_not_import_main_module,
    test_engine_visible_after_python_m_start,
    test_scan_fast_button_end_to_end,
]
