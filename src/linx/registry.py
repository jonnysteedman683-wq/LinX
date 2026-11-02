"""The system registry: what each repo is, where it sits, how to check it."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

LAYERS = ("orchestration", "execution", "transport", "memory", "interface")
ROLES = ("owner", "orchestrator", "worker", "watcher")


class RegistryError(ValueError):
    """The registry file is malformed. Nothing is loaded when this is raised."""


@dataclass(frozen=True)
class System:
    name: str
    repo: str
    lang: str
    layer: str
    does: str
    role: str | None = None
    checks: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    # Nightly loop: which agent works this repo's backlog, and (optionally) the unattended command
    # LinX runs for it. Placeholders: {item} (backlog id), {repo} (checkout path).
    nightly_agent: str | None = None
    nightly_command: tuple[str, ...] = ()

    def slug(self, owner: str) -> str:
        return f"{owner}/{self.repo}"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> System:
        for key in ("name", "repo", "lang", "layer", "does"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise RegistryError(f"system is missing {key!r}: {dict(raw)!r}")
        if raw["layer"] not in LAYERS:
            raise RegistryError(f"{raw['name']}: unknown layer {raw['layer']!r}")
        role = raw.get("role")
        if role is not None and role not in ROLES:
            raise RegistryError(f"{raw['name']}: unknown role {role!r}")
        checks = raw.get("checks", [])
        if not all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in checks):
            raise RegistryError(f"{raw['name']}: each check must be a non-empty argv list")
        nightly = raw.get("nightly") or {}
        if not isinstance(nightly, Mapping):
            raise RegistryError(f"{raw['name']}: nightly must be an object")
        agent = nightly.get("agent")
        command = nightly.get("command", [])
        if agent is not None and (not isinstance(agent, str) or not agent):
            raise RegistryError(f"{raw['name']}: nightly.agent must be a non-empty string")
        if not isinstance(command, list) or not all(isinstance(a, str) for a in command):
            raise RegistryError(f"{raw['name']}: nightly.command must be an argv list")
        if command and agent is None:
            raise RegistryError(f"{raw['name']}: nightly.command needs nightly.agent")
        return cls(
            raw["name"],
            raw["repo"],
            raw["lang"],
            raw["layer"],
            raw["does"],
            role,
            tuple(tuple(c) for c in checks),
            agent,
            tuple(command),
        )


@dataclass(frozen=True)
class Registry:
    owner: str
    systems: tuple[System, ...]

    def __iter__(self) -> Iterator[System]:
        return iter(self.systems)

    def __len__(self) -> int:
        return len(self.systems)

    def get(self, name: str) -> System:
        for s in self.systems:
            if s.name == name:
                return s
        raise KeyError(name)

    def by_layer(self) -> dict[str, list[System]]:
        return {layer: [s for s in self.systems if s.layer == layer] for layer in LAYERS}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Registry:
        if raw.get("version") != 1:
            raise RegistryError(f"unsupported registry version {raw.get('version')!r}")
        owner = raw.get("owner")
        if not isinstance(owner, str) or not owner:
            raise RegistryError("registry is missing 'owner'")
        systems = tuple(System.from_dict(s) for s in raw.get("systems", []))
        names = [s.name for s in systems]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise RegistryError(f"duplicate system names: {dupes}")
        return cls(owner, systems)


def load(path: str | Path | None = None) -> Registry:
    """Load a registry file, or the bundled one when no path is given."""
    if path is None:
        text = resources.files("linx").joinpath("systems.json").read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry is not valid JSON: {exc}") from exc
    return Registry.from_dict(raw)
