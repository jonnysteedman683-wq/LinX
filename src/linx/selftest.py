"""`linx selftest`: prove the LinX chain works on this machine, read-only.

Steps (each PASS / FAIL / SKIP, with a fix hint on FAIL):
1. unisync   Lestinxor's UniSync is installed and knows every LinX agent.
2. relay     A built-in Vortex feed relays into a fresh ledger that verifies and reads back in status.
3. checkouts Which repo checkouts exist under --root (informational).
4. vortex    The real Vortex answers `/events` with the operator token; its events relay into a
             throwaway in-memory ledger that verifies. Nothing is written anywhere.
5. supabase  The shared ledger syncs and its whole hash chain verifies; the LinX agents are registered
             in `unisync_agents`. Read-only.
Steps 4 and 5 are skipped when their settings are absent. Exit 0 when nothing failed.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linx import relay
from linx.doctor import doctor
from linx.registry import Registry

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"

SAMPLE_FEED: list[dict[str, Any]] = [
    {
        "event_id": 1,
        "task_id": "st-1",
        "actor_id": "ops",
        "event_type": "created",
        "details": {"requires_approval": False},
        "task_goal": "selftest",
    },
    {"event_id": 2, "task_id": "st-1", "actor_id": "hermes-1", "event_type": "claimed", "details": {}},
    {
        "event_id": 3,
        "task_id": "st-1",
        "actor_id": "hermes-1",
        "event_type": "blocked",
        "details": {"blocked_reason": "selftest"},
    },
    {
        "event_id": 4,
        "task_id": "st-1",
        "actor_id": "hermes-1",
        "event_type": "completed",
        "details": {"result": "ok"},
    },
]


@dataclass(frozen=True)
class Step:
    name: str
    result: str
    detail: str
    fix: str = ""


def _agents_needed(registry: Registry) -> set[str]:
    return {s.name for s in registry if s.role}


def check_unisync(registry: Registry) -> Step:
    try:
        from lestinxor.unisync import DEFAULT_AGENTS
    except ImportError:
        return Step("unisync", FAIL, "lestinxor is not installed", 'pip install -e ".[unisync]"')
    missing = sorted(_agents_needed(registry) - {c.name for c in DEFAULT_AGENTS})
    if missing:
        return Step(
            "unisync",
            FAIL,
            f"installed lestinxor lacks agents: {', '.join(missing)}",
            'upgrade it: pip install --upgrade --force-reinstall ".[unisync]"',
        )
    return Step("unisync", PASS, f"{len(DEFAULT_AGENTS)} agents known, all LinX agents present")


def check_relay(registry: Registry, root: Path) -> Step:
    try:
        from lestinxor.unisync import Ledger

        from linx import status

        lg = Ledger()
        n = relay.relay(lg, SAMPLE_FEED)
        replay = Ledger.from_jsonl(lg.to_jsonl())
        again = relay.relay(replay, SAMPLE_FEED)
        fleet = status.build(registry, root, replay, "selftest")
    except ImportError:
        return Step("relay", SKIP, "needs lestinxor (see unisync)")
    except Exception as exc:
        return Step("relay", FAIL, f"{type(exc).__name__}: {exc}", "report this: the relay itself is broken")
    ok = (
        n == 4
        and again == 0
        and replay.verify()
        and fleet.verified
        and fleet.vortex == {"completed": 1}
        and len(fleet.escalations) == 1
    )
    if not ok:
        return Step(
            "relay",
            FAIL,
            f"relayed {n}, re-relayed {again}, vortex {fleet.vortex}",
            "report this: the relay itself is broken",
        )
    return Step("relay", PASS, "4 events relayed, replay verifies, re-run adds nothing, status reads it")


def check_checkouts(registry: Registry, root: Path) -> Step:
    reports = doctor(registry, root)
    present = [r.system for r in reports if r.present]
    missing = [r.system for r in reports if not r.present]
    detail = f"{len(present)}/{len(reports)} under {root}"
    if missing:
        detail += f" (missing: {', '.join(missing)})"
    return Step("checkouts", INFO, detail)


def check_owner(registry: Registry) -> Step:
    from linx import owner

    root, start = owner.pinned(registry)
    path = owner.seed_path()
    if not root:
        return Step(
            "owner",
            INFO,
            "no owner key pinned: approvals and halts are checked by name only "
            "(`linx owner init`, then `linx owner pin`)",
        )
    if not path.exists():
        return Step(
            "owner",
            INFO,
            f"key {root[:12]}... enforced from #{start}; this machine has no seed (cannot sign)",
        )
    try:
        mine = owner.load_key(path).root
    except (OSError, ValueError) as exc:
        return Step(
            "owner", FAIL, f"seed at {path} is unreadable: {exc}", "restore the seed from your backup"
        )
    if mine != root:
        return Step(
            "owner",
            FAIL,
            f"seed at {path} does not match the pinned key {root[:12]}...",
            "restore the right seed, or pin this one with `linx owner pin`",
        )
    return Step("owner", PASS, f"key {root[:12]}... enforced from #{start}; this machine can sign")


def check_vortex(url: str, token: str, fetch_factory: Callable[[str, str], Any] | None = None) -> Step:
    if not url or not token:
        return Step("vortex", SKIP, "set LINX_VORTEX_URL (or --vortex-url) and VORTEX_OPERATOR_TOKEN to test")
    from linx import live

    try:
        from lestinxor.unisync import Ledger

        fetch = (fetch_factory or live.http_fetch)(url, token)
        events = fetch(0)
        lg = Ledger()
        n = relay.relay(lg, events)
    except ImportError:
        return Step("vortex", SKIP, "needs lestinxor (see unisync)")
    except live.LiveError as exc:
        return Step("vortex", FAIL, str(exc), "check the URL (https, or http to loopback) and token")
    except Exception as exc:
        hint = "is Vortex running, and is this token in VORTEX_OPERATOR_IDS?"
        if getattr(exc, "code", None) in (401, 403):
            hint = "the token is wrong or not an operator: check VORTEX_OPERATOR_IDS on the Vortex side"
        return Step("vortex", FAIL, f"{type(exc).__name__}: {exc}", hint)
    if not lg.verify():
        return Step("vortex", FAIL, "relayed ledger does not verify", "report this")
    return Step(
        "vortex", PASS, f"{url} answered with {len(events)} event(s); {n} relayed into a throwaway ledger"
    )


def _get_json(url: str, key: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def check_supabase(
    registry: Registry,
    url: str,
    key: str,
    ledger_factory: Callable[[], Any] | None = None,
    get_json: Callable[[str, str], Any] = _get_json,
) -> Step:
    if not url or not key:
        return Step("supabase", SKIP, "set UNISYNC_SUPABASE_URL and UNISYNC_SUPABASE_KEY to test")
    try:
        if ledger_factory is None:
            from lestinxor.unisync_supabase import SupabaseLedger

            from linx import owner

            remote = SupabaseLedger(url, key, **owner.ledger_kwargs(registry, sign=False))
        else:
            remote = ledger_factory()
        remote.sync()
        verified = remote.ledger.verify()
        rows = get_json(url.rstrip("/") + "/rest/v1/unisync_agents?select=name", key) or []
    except ImportError:
        return Step("supabase", SKIP, "needs lestinxor (see unisync)")
    except Exception as exc:
        hint = "check the URL and that the key is the service-role key"
        if "ProtocolError" in type(exc).__name__:
            hint = (
                "a row breaks the protocol: if it names an unknown sender, re-run unisync/schema.sql "
                "and upgrade lestinxor; otherwise the ledger may be tampered with"
            )
        return Step("supabase", FAIL, f"{type(exc).__name__}: {exc}", hint)
    if not verified:
        return Step("supabase", FAIL, "hash chain does not verify", "the shared ledger may be tampered with")
    names = {r.get("name") for r in rows if isinstance(r, dict)}
    missing = sorted(_agents_needed(registry) - names)
    entries = len(remote.ledger.entries)
    if missing:
        return Step(
            "supabase",
            FAIL,
            f"{entries} entries verify, but agents not registered: {', '.join(missing)}",
            "run Lestinxor's unisync/schema.sql in the Supabase SQL editor (safe to re-run)",
        )
    return Step("supabase", PASS, f"{entries} entries verify; all LinX agents registered")


def run(
    registry: Registry, root: Path, vortex_url: str, token: str, supabase_url: str, supabase_key: str
) -> list[Step]:
    return [
        check_unisync(registry),
        check_relay(registry, root),
        check_checkouts(registry, root),
        check_owner(registry),
        check_vortex(vortex_url, token),
        check_supabase(registry, supabase_url, supabase_key),
    ]


def render(steps: list[Step]) -> str:
    lines = []
    for s in steps:
        lines.append(f"[{s.result}] {s.name:<10} {s.detail}")
        if s.result == FAIL and s.fix:
            lines.append(f"{'':18}fix: {s.fix}")
    failed = sum(s.result == FAIL for s in steps)
    skipped = [s.name for s in steps if s.result == SKIP]
    verdict = "LinX works on this machine" if not failed else f"{failed} check(s) failed"
    if not failed and skipped:
        verdict += f" (not tested: {', '.join(skipped)})"
    lines.append(f"\n{verdict}")
    return "\n".join(lines)
