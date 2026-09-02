"""app/analysis/modules — модули анализа («тентакли»).

Каждый модуль — чистая функция ``MarketFeatures → Iterable[Factor]``.
Импорт этого пакета регистрирует все модули в ``app.analysis.registry``.
"""

from __future__ import annotations

from app.analysis.modules import (context, derivatives, levels, momentum,  # noqa: F401
                                  potential, quality, sentiment, smc,
                                  structure, trend, volume)
