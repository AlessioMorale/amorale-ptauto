"""Device/cable knowledge, reused from the MCP Packet Tracer project.

ptauto does not keep a second copy of the model catalog: the port names,
categories and cabling rules come from `packet_tracer_mcp`, which verified them
against a live PT. This module is only the thin adapter that the rest of ptauto
talks to, so a catalog change upstream needs no edit here.
"""

from __future__ import annotations

from dataclasses import dataclass

from packet_tracer_mcp.infrastructure.catalog.cables import (
    CABLE_TYPES,
    infer_cable as _infer_cable,
)
from packet_tracer_mcp.infrastructure.catalog.devices import (
    ALL_MODELS,
    DeviceModel,
    category_of_model,
    resolve_model,
)

from .errors import SpecError

# Aliases people actually type. Same table as the MCP adapter's, which lives
# inside a closure there and so cannot be imported.
CABLE_ALIASES: dict[str, str] = {
    "crossover": "cross",
    "cross-over": "cross",
    "copper-crossover": "cross",
    "copper-straight": "straight",
    "straight-through": "straight",
    "rollover": "roll",
    "dce": "serial",
    "serial-dce": "serial",
}

# Categories that speak Cisco IOS (configured by pushing CLI) versus categories
# configured through the PT object model (IP/mask/gateway/DNS on the host).
IOS_CATEGORIES = frozenset({"router", "switch", "accesspoint", "firewall"})
HOST_CATEGORIES = frozenset({"pc", "server", "laptop", "tablet", "phone", "iot"})


@dataclass(frozen=True)
class ModelInfo:
    """What ptauto needs to know about a device model."""

    pt_type: str
    category: str
    ports: tuple[str, ...]

    @property
    def is_ios(self) -> bool:
        return self.category in IOS_CATEGORIES

    @property
    def is_host(self) -> bool:
        return self.category in HOST_CATEGORIES


def model_info(model: str) -> ModelInfo:
    """Resolve a model name (or alias) to its catalog entry.

    Raises SpecError with the closest matches, because a typo in a model name is
    by far the most common thing to get wrong in the YAML.
    """
    resolved: DeviceModel | None = resolve_model(model)
    if resolved is None:
        raise SpecError(
            f"Unknown device model {model!r}. "
            f"Close matches: {', '.join(suggest_models(model)) or 'none'}. "
            f"Run `ptauto models` for the full catalog."
        )
    return ModelInfo(
        pt_type=resolved.pt_type,
        category=resolved.category,
        ports=tuple(p.full_name for p in resolved.ports),
    )


def suggest_models(model: str, limit: int = 5) -> list[str]:
    """Catalog entries whose name looks like `model`, for error messages."""
    import difflib

    names = sorted(ALL_MODELS.keys())
    close = difflib.get_close_matches(model, names, n=limit, cutoff=0.4)
    if close:
        return close
    needle = model.lower()
    return [n for n in names if needle in n.lower()][:limit]


def list_models() -> dict[str, list[str]]:
    """Catalog grouped by category, for `ptauto models`."""
    grouped: dict[str, list[str]] = {}
    for name, dev in sorted(ALL_MODELS.items()):
        grouped.setdefault(dev.category, []).append(name)
    return grouped


def resolve_cable(cable: str | None) -> str | None:
    """Normalise a cable name; None means 'let ptauto infer it'."""
    if not cable:
        return None
    value = CABLE_ALIASES.get(cable.lower(), cable.lower())
    if value not in CABLE_TYPES:
        raise SpecError(
            f"Unknown cable type {cable!r}. Valid: {', '.join(sorted(CABLE_TYPES))}. "
            f"Aliases: crossover->cross, rollover->roll."
        )
    return value


def infer_cable_for(category_a: str, category_b: str) -> str:
    """Cable PT expects between two device categories."""
    return _infer_cable(category_a, category_b)


def category_for_pt_model(pt_model: str) -> str:
    """Category of a model name as PT itself reports it (`getModel()`)."""
    return category_of_model(pt_model)


def normalise_port(port: str) -> str:
    """Canonical port spelling, so `g0/0` and `GigabitEthernet0/0` compare equal."""
    raw = port.strip()
    lowered = raw.lower()
    prefixes = (
        ("gigabitethernet", "GigabitEthernet"),
        ("gigabit", "GigabitEthernet"),
        ("gige", "GigabitEthernet"),
        ("gig", "GigabitEthernet"),
        ("gi", "GigabitEthernet"),
        ("fastethernet", "FastEthernet"),
        ("fast", "FastEthernet"),
        ("fa", "FastEthernet"),
        ("ethernet", "Ethernet"),
        ("serial", "Serial"),
        ("se", "Serial"),
        ("vlan", "Vlan"),
        ("wireless", "Wireless"),
        # Single-letter shorthands last: they would otherwise swallow the longer
        # spellings above ("gi0/1" must not become GigabitEthernet"i0/1").
        ("g", "GigabitEthernet"),
        ("f", "FastEthernet"),
        ("s", "Serial"),
        ("e", "Ethernet"),
    )
    for short, full in prefixes:
        if lowered.startswith(short):
            suffix = raw[len(short):]
            # "g0/0" -> GigabitEthernet0/0, but do not rewrite "gateway0" style
            # names that happen to share a prefix and carry no digits.
            if suffix and (suffix[0].isdigit() or suffix[0] in "/ "):
                return full + suffix.strip()
    return raw
