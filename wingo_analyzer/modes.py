"""The four games = the four nodes of the desk.

type ids verified against the live API (GetGameIssue / GetNoaverageEmerdList).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    type_id: int
    seconds: int
    node: int
    ai: str          # lead analyst persona: ROB or ROD
    accent: str      # hex accent used by the snapshot renderer

    @property
    def poll_seconds(self) -> float:
        # 30s game polls every 5s, 1min every 10s, 3/5min every 20s
        return max(5.0, min(20.0, self.seconds / 6.0))

    def node_label(self) -> str:
        return f"NODE {self.node:02d}"


MODES: dict[str, Mode] = {
    "30sec": Mode("30sec", "30 SECOND", 30, 30, 1, "ROB", "#ff8a3d"),
    "1min": Mode("1min", "1 MINUTE", 1, 60, 2, "ROD", "#35d0a5"),
    "3min": Mode("3min", "3 MINUTE", 2, 180, 3, "ROB", "#6ea8ff"),
    "5min": Mode("5min", "5 MINUTE", 3, 300, 4, "ROD", "#c084fc"),
}

ORDER: list[str] = list(MODES.keys())


def get_mode(key: str) -> Mode | None:
    return MODES.get((key or "").strip().lower())


def mode_for_type_id(type_id: int) -> Mode | None:
    for mode in MODES.values():
        if mode.type_id == type_id:
            return mode
    return None


def parse_mode_arg(raw: str | None) -> Mode | None:
    """Accept '1min', '1m', 'node 2', '2', '30sec', '30s' …"""
    if not raw:
        return None
    value = raw.strip().lower()
    if value in MODES:
        return MODES[value]
    if value.startswith("node"):
        value = value[4:].strip()
    if value.isdigit():
        for mode in MODES.values():
            if str(mode.node) == value or str(mode.type_id) == value:
                return mode
        return None
    aliases = {
        "1m": "1min", "3m": "3min", "5m": "5min", "30s": "30sec",
        "30": "30sec", "1": "1min", "2": "3min", "3": "5min",
    }
    return MODES.get(aliases.get(value, value))


# number -> colours, exactly the way the API reports them
GREEN = {1, 3, 7, 9}
RED = {2, 4, 6, 8}
VIOLET = {0, 5}


def colours_for(number: int) -> list[str]:
    if number in (0, 5):
        return ["violet", "green" if number == 5 else "red"]
    return ["green" if number in GREEN else "red"]


def size_for(number: int) -> str:
    return "BIG" if number >= 5 else "SMALL"


def parity_for(number: int) -> str:
    return "ODD" if number % 2 else "EVEN"


COLOUR_EMOJI = {"green": "🟢", "red": "🔴", "violet": "🟣"}
COLOUR_HEX = {"green": "#35d0a5", "red": "#ff5c6c", "violet": "#a06bff"}
