from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SUPPORTED_TYPES = {"line", "histogram", "marker"}
SUPPORTED_PANES = {"price", "strategy"}


@dataclass(frozen=True)
class IndicatorDefinition:
    key: str
    name: str
    type: str
    pane: str
    stream_key: str = ""
    color: str = ""
    unit: str = ""
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class IndicatorFrame:
    values: dict[str, float | None] = field(default_factory=dict)
    markers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_indicator_definitions(raw: object) -> list[IndicatorDefinition]:
    if raw in (None, {}, []):
        return []
    if not isinstance(raw, dict):
        raise ValueError("INDICATORS must be a dict keyed by indicator key")

    out: list[IndicatorDefinition] = []
    seen: set[str] = set()
    for raw_key, raw_cfg in raw.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("indicator key must be a non-empty string")
        key = raw_key.strip()
        if key in seen:
            raise ValueError(f"indicator {key} is duplicated")
        seen.add(key)
        if not isinstance(raw_cfg, dict):
            raise ValueError(f"indicator {key} config must be a dict")

        typ = str(raw_cfg.get("type", "")).strip().lower()
        pane = str(raw_cfg.get("pane", "")).strip().lower()
        if typ not in SUPPORTED_TYPES:
            raise ValueError(f"indicator {key} type must be one of: histogram, line, marker")
        if pane not in SUPPORTED_PANES and not pane.startswith("custom:"):
            raise ValueError(f"indicator {key} pane must be price, strategy, or custom:<name>")

        config = raw_cfg.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError(f"indicator {key} config.config must be a dict")
        out.append(IndicatorDefinition(
            key=key,
            name=str(raw_cfg.get("name") or key).strip(),
            type=typ,
            pane=pane,
            color=str(raw_cfg.get("color") or "").strip(),
            unit=str(raw_cfg.get("unit") or "").strip(),
            description=str(raw_cfg.get("description") or "").strip(),
            config=dict(config),
        ))
    return out


class IndicatorWriter:
    """Per-bar strategy output shared by Hosted workers and offline replay."""

    def __init__(self, definitions: list[IndicatorDefinition]) -> None:
        self._definitions = {definition.key: definition for definition in definitions}
        self._frame = IndicatorFrame()

    def reset_bar(self) -> None:
        self._frame = IndicatorFrame()

    def set(self, key: str, value: float | int | None) -> None:
        key = str(key or "").strip()
        definition = self._definitions.get(key)
        if definition is None:
            self._frame.warnings.append(f"undeclared indicator key ignored: {key}")
            return
        if definition.type == "marker":
            self._frame.warnings.append(f"marker indicator key ignored by set(): {key}")
            return
        if value is None:
            self._frame.values[key] = None
            return
        try:
            self._frame.values[key] = float(value)
        except (TypeError, ValueError):
            self._frame.warnings.append(f"indicator value must be numeric or None: {key}")

    def mark(self, key: str, text: str = "", price: float | None = None, color: str = "") -> None:
        key = str(key or "").strip()
        definition = self._definitions.get(key)
        if definition is None:
            self._frame.warnings.append(f"undeclared indicator key ignored: {key}")
            return
        if definition.type != "marker":
            self._frame.warnings.append(f"non-marker indicator key ignored by mark(): {key}")
            return
        marker: dict[str, Any] = {"text": str(text or "")}
        if price is not None:
            try:
                marker["price"] = float(price)
            except (TypeError, ValueError):
                self._frame.warnings.append(f"marker price must be numeric: {key}")
                return
        color = str(color or "").strip()
        if color:
            marker["color"] = color
        self._frame.markers.setdefault(key, []).append(marker)

    def drain(self) -> IndicatorFrame:
        frame = self._frame
        self.reset_bar()
        return frame
