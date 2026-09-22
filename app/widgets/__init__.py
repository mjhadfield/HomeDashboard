"""Widget registry: every module in this package that exposes a `widget` is picked up automatically."""
from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import Widget

log = logging.getLogger("hub.widgets")
_registry: dict[str, Widget] = {}


def discover() -> dict[str, Widget]:
    if _registry:
        return _registry
    for mod in pkgutil.iter_modules(__path__):
        if mod.name == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception:  # one broken widget must not take the dashboard down
            log.exception("widget module %s failed to import", mod.name)
            continue
        if isinstance(w := getattr(module, "widget", None), Widget):
            _registry[w.id] = w
    return _registry


def get(widget_id: str) -> Widget | None:
    return discover().get(widget_id)
