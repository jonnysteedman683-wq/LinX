"""Bridges to the tools that already join the systems: UniSync and Sebastian crosstest."""

from __future__ import annotations

from typing import Any

from linx.registry import Registry


def unisync_gaps(registry: Registry, registered: set[str]) -> list[str]:
    """Systems that have a UniSync role in LinX but are not registered agents on the ledger yet."""
    return sorted(s.name for s in registry if s.role and s.name not in registered)


def unisync_agents() -> set[str]:
    """Agent names from Lestinxor's reference UniSync (`pip install linx[unisync]`)."""
    from lestinxor.unisync import DEFAULT_AGENTS

    return {card.name for card in DEFAULT_AGENTS}


def unisync_sql(registry: Registry, registered: set[str]) -> str:
    """Idempotent SQL that registers the missing agents in the shared Supabase ledger."""
    rows = [
        f"    ('{s.name}', '{s.role}', '{{{s.slug(registry.owner)}}}')"
        for s in registry
        if s.role and s.name not in registered
    ]
    if not rows:
        return "-- every LinX system with a role is already a UniSync agent\n"
    return (
        "insert into public.unisync_agents (name, role, repos) values\n"
        + ",\n".join(rows)
        + "\non conflict (name) do nothing;\n"
    )


def crosstest_manifest(registry: Registry, commands: dict[str, list[str]]) -> dict[str, Any]:
    """A `sebastian crosstest` manifest for the systems that have a non-interactive run command."""
    agents = [
        {"name": s.name, "suite": f"{s.repo}/evals/crosstest", "command": commands[s.name], "timeout": 600}
        for s in registry
        if s.name in commands
    ]
    return {"version": 1, "agents": agents}


# Non-interactive run commands that the systems document today. Add one when a system ships one.
KNOWN_COMMANDS: dict[str, list[str]] = {
    "sebastian": ["sebastian", "run", "--unattended", "--workspace", "{workspace}", "{task}"],
}
