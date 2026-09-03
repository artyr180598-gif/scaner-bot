"""Регистрация обработчиков."""

from .start import router as start_router
from .find import router as find_router
from .analyze import router as analyze_router
from .settings import router as settings_router


def all_routers() -> list:
    return [start_router, find_router, analyze_router, settings_router]
