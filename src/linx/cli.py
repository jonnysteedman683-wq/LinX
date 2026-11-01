"""`linx` command line."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from linx import bridges
from linx.doctor import doctor
from linx.registry import LAYERS, RegistryError, load


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="linx", description="Control plane over Jonny's agent systems.")
    p.add_argument("--registry", help="registry JSON (default: the bundled systems.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("systems", help="list every system by layer")
    d = sub.add_parser("doctor", help="which checkouts exist; --checks also runs each system's checks")
    d.add_argument("--root", default="..", help="folder holding all the repo checkouts (default: ..)")
    d.add_argument("--checks", action="store_true", help="run each system's checks (slow)")
    u = sub.add_parser("unisync", help="systems missing from the UniSync ledger, as SQL to register them")
    u.add_argument("--registered", help="comma-separated agent names (default: read from lestinxor)")
    sub.add_parser("crosstest", help="print a `sebastian crosstest` manifest for systems that support it")
    r = sub.add_parser("relay", help="mirror a Vortex event feed (JSON array) onto a UniSync JSONL ledger")
    r.add_argument("events", help="JSON array from vortex_operator_events")
    r.add_argument(
        "--ledger", required=True, help="UniSync JSONL file; created if missing, appended in place"
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reg = load(args.registry)
    except (RegistryError, OSError) as exc:
        print(f"linx: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "systems":
        groups = reg.by_layer()
        for layer in LAYERS:
            print(f"{layer}:")
            for s in groups[layer]:
                print(f"  {s.name:<17} {s.repo:<20} {s.lang:<10} {s.does}")
        return 0

    if args.cmd == "doctor":
        reports = doctor(reg, Path(args.root).resolve(), run_checks=args.checks)
        for r in reports:
            if not r.present:
                state = "MISSING"
            elif r.missing_tools:
                state = "no " + ",".join(r.missing_tools)
            elif r.check_codes:
                state = "pass" if r.ok else "FAIL " + ",".join(map(str, r.check_codes))
            else:
                state = "present"
            print(f"{r.system:<17} {r.head or '-':<8} {state}")
        return 0 if all(r.present for r in reports) and (not args.checks or all(r.ok for r in reports)) else 1

    if args.cmd == "unisync":
        if args.registered is not None:
            registered = {n.strip() for n in args.registered.split(",") if n.strip()}
        else:
            try:
                registered = bridges.unisync_agents()
            except ImportError:
                print("linx: install `linx[unisync]` or pass --registered", file=sys.stderr)
                return 2
        print(bridges.unisync_sql(reg, registered), end="")
        return 0

    if args.cmd == "relay":
        return _relay(Path(args.events), Path(args.ledger))

    print(json.dumps(bridges.crosstest_manifest(reg, bridges.KNOWN_COMMANDS), indent=2))
    return 0


def _relay(events_path: Path, ledger_path: Path) -> int:
    try:
        from lestinxor.unisync import Ledger, ProtocolError
    except ImportError:
        print("linx: install `linx[unisync]` to relay", file=sys.stderr)
        return 2
    from linx.relay import RelayError, cursor, relay

    try:
        events = json.loads(events_path.read_text(encoding="utf-8"))
        if not isinstance(events, list):
            raise RelayError("events file must hold a JSON array")
        text = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
        ledger = Ledger.from_jsonl(text)
        n = relay(ledger, events)
    except (OSError, json.JSONDecodeError, RelayError, ProtocolError) as exc:
        print(f"linx: {exc}", file=sys.stderr)
        return 2
    ledger_path.write_text(ledger.to_jsonl(), encoding="utf-8")
    print(f"relayed {n} event(s); cursor {cursor(ledger)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
