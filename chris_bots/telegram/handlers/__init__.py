"""Handlers: start, scan, analyze, top, settings."""
from .start import router as start_router
from .scan import router as scan_router
from .analyze import router as analyze_router
from .top import router as top_router
from .settings import router as settings_router


def all_routers():
    return [start_router, scan_router, analyze_router, top_router, settings_router]
