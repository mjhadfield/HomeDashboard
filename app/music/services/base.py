"""What a music service declares about itself, so Settings can show it and the page knows whether it can be used."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import store


class ServiceError(RuntimeError):
    """Something the person should see, in plain words ("Last.fm says the API key is wrong")."""


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    secret: bool = False
    placeholder: str = ""
    required: bool = True


@dataclass
class Service:
    id: str = ""
    name: str = ""
    blurb: str = ""                  # one line for Settings: what it's used for
    link: str = ""                   # where to get a key, if it needs one
    order: int = 50
    fields: tuple[Field, ...] = field(default_factory=tuple)
    always_on: bool = False          # can't be switched off (e.g. reading your own library)

    # ---- settings ----------------------------------------------------------------------------------------------
    def cfg(self) -> dict[str, Any]:
        return store.service_cfg(self.id)

    def enabled(self) -> bool:
        return self.always_on or bool(self.cfg().get("enabled", True))

    def configured(self, cfg: dict[str, Any] | None = None) -> bool:
        cfg = self.cfg() if cfg is None else cfg
        return all(cfg.get(f.name) for f in self.fields if f.required)

    def ready(self) -> bool:
        return self.enabled() and self.configured()

    def secrets(self) -> list[str]:
        cfg = self.cfg()
        return [str(cfg[f.name]) for f in self.fields if f.secret and cfg.get(f.name)]

    def public(self) -> dict[str, Any]:
        """What Settings may see: secrets only as 'there is one'."""
        cfg = self.cfg()
        values = {f.name: ("" if f.secret else cfg.get(f.name, "")) for f in self.fields}
        has = {f.name: bool(cfg.get(f.name)) for f in self.fields if f.secret}
        return {"id": self.id, "name": self.name, "blurb": self.blurb, "link": self.link, "always_on": self.always_on,
                "enabled": self.enabled(), "configured": self.configured(cfg), "values": values, "has": has,
                "fields": [{"name": f.name, "label": f.label, "secret": f.secret, "placeholder": f.placeholder, "required": f.required} for f in self.fields],
                **self.extra()}

    def merge(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        """Validate an update from Settings. A blank secret keeps the stored one."""
        names = {f.name for f in self.fields}
        if unknown := set(new) - names - {"enabled"}:
            raise ValueError(f"{self.name}: unknown setting(s) {', '.join(sorted(unknown))}")
        out = dict(old)
        for f in self.fields:
            if f.name not in new:
                continue
            v = str(new[f.name] or "").strip()
            if f.secret and not v:
                continue
            if len(v) > 300:
                raise ValueError(f"{self.name}: {f.label} is too long")
            out[f.name] = v
        if "enabled" in new:
            out["enabled"] = bool(new["enabled"])
        return out

    # ---- for subclasses ----------------------------------------------------------------------------------------
    def extra(self) -> dict[str, Any]:
        """Anything else Settings should show (e.g. the Spotify login's state)."""
        return {}

    async def test(self) -> str:
        """Try it for real; return a short description of what worked, or raise ServiceError."""
        return "OK"
