"""Outside services the Music page can use. Every module in this package that exposes `service` is picked up, in the
order of its `order`; Settings → Music page lists them, with their own fields, a Test button and an on/off switch.

Adding one (say Deezer, ListenBrainz or Discogs): write a module with a Service subclass describing its fields and a
`test()`, plus whatever client functions it needs; then use it from an ingredient (ingredients.py) or from Discover.
Nothing else needs to know it exists."""
from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import Service

log = logging.getLogger("hub.music")
_registry: dict[str, Service] = {}


def all_services() -> dict[str, Service]:
    if _registry:
        return _registry
    found = []
    for mod in pkgutil.iter_modules(__path__):
        if mod.name == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception:  # one broken service must not take the page down
            log.exception("music service %s failed to import", mod.name)
            continue
        if isinstance(getattr(module, "service", None), Service):
            found.append(module.service)
    for s in sorted(found, key=lambda s: (s.order, s.id)):
        _registry[s.id] = s
    return _registry


def get(service_id: str) -> Service:
    return all_services()[service_id]
