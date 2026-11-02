"""`linx` command line."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from linx import bridges
from linx.doctor import doctor
from linx.registry import LAYERS, Registry, RegistryError, load


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
    st = sub.add_parser("status", help="checkouts + UniSync: task load, approvals, escalations, Vortex tasks")
    st.add_argument("--root", default="..", help="folder holding all the repo checkouts (default: ..)")
    st.add_argument("--ledger", help="UniSync JSONL file (default: Supabase if UNISYNC_SUPABASE_URL is set)")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    sf = sub.add_parser("selftest", help="read-only check that the whole chain works on this machine")
    sf.add_argument("--root", default="..", help="folder holding all the repo checkouts (default: ..)")
    sf.add_argument(
        "--vortex-url", help="Vortex root (default: $LINX_VORTEX_URL; token: $VORTEX_OPERATOR_TOKEN)"
    )
    mem = sub.add_parser("memory", help="shared UniSync memory: read, write, check, consolidate, vault cards")
    mem.add_argument("--ledger", help="UniSync JSONL file (default: Supabase via UNISYNC_SUPABASE_URL/_KEY)")
    msub = mem.add_subparsers(dest="mcmd", required=True)
    mg = msub.add_parser("get", help="current value of scope/key, with author")
    mg.add_argument("ref", help="scope/key (scope defaults to 'shared' when there is no '/')")
    mh = msub.add_parser("history", help="every write to scope/key")
    mh.add_argument("ref")
    mp = msub.add_parser("put", help="write a fact (value is JSON if it parses, else text)")
    mp.add_argument("key")
    mp.add_argument("value")
    mp.add_argument("--as", dest="sender", required=True, help="the registered agent writing it")
    mp.add_argument("--scope", default="shared")
    mc = msub.add_parser("check", help="conflicts, stale facts, duplicates and links (read-only)")
    mc.add_argument("--stale-after", type=int, default=500, help="ledger entries before a fact is stale")
    mc.add_argument("--json", action="store_true")
    mk = msub.add_parser("cards", help="write HORIZONTAL-VAULT cards to a staging folder")
    mk.add_argument("--out", required=True, help="staging folder (not the live vault)")
    mo = msub.add_parser("consolidate", help="check, then post the summary and escalate new conflicts")
    mo.add_argument("--as", dest="sender", required=True, help="agent posting it, e.g. coordinator-bot")
    mo.add_argument("--stale-after", type=int, default=500)
    mo.add_argument("--every", type=float, help="repeat every N seconds (keeps memory active)")
    mo.add_argument("--log-file", help="append logs here (UTF-8) instead of stderr")
    ng = sub.add_parser("nightly", help="nightly loop: plan backlog tasks, run unattended agents, digest")
    ng.add_argument("--ledger", help="UniSync JSONL file (default: Supabase via UNISYNC_SUPABASE_URL/_KEY)")
    ng.add_argument("--root", default="..", help="folder holding all the repo checkouts (default: ..)")
    ng.add_argument("--date", help="day to plan/report, YYYY-MM-DD (default: today)")
    nsub = ng.add_subparsers(dest="ncmd", required=True)
    npl = nsub.add_parser("plan", help="post one task per repo for its next unlocked backlog item")
    npl.add_argument(
        "--as", dest="sender", default="lestinxor", help="orchestrator posting (default lestinxor)"
    )
    nw = nsub.add_parser("work", help="claim and run one open task for an agent with an unattended command")
    nw.add_argument("--as", dest="sender", required=True, help="the worker agent, e.g. sebastian")
    nw.add_argument("--timeout", type=float, default=3600.0)
    nd = nsub.add_parser("digest", help="markdown morning report")
    nd.add_argument("--out", help="also write the digest to this file")
    nr = nsub.add_parser("run", help="plan, then work every unattended agent once, then digest")
    nr.add_argument("--as", dest="sender", default="lestinxor")
    nr.add_argument("--timeout", type=float, default=3600.0)
    nr.add_argument("--out", help="also write the digest to this file")
    lv = sub.add_parser("relay-live", help="poll Vortex's /events feed and relay new events continuously")
    lv.add_argument("--vortex-url", required=True, help="Vortex server root (token: $VORTEX_OPERATOR_TOKEN)")
    lv.add_argument("--ledger", help="local JSONL ledger (default: Supabase via $UNISYNC_SUPABASE_URL/_KEY)")
    lv.add_argument("--interval", type=float, default=5.0, help="seconds between polls (default 5)")
    lv.add_argument("--once", action="store_true", help="poll once and exit (for cron / scheduled runs)")
    lv.add_argument("--log-file", help="append logs here (UTF-8) instead of stderr")
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
    if args.cmd == "status":
        return _status(reg, Path(args.root).resolve(), args.ledger, args.json)
    if args.cmd == "selftest":
        return _selftest(reg, Path(args.root).resolve(), args.vortex_url)
    if args.cmd == "memory":
        return _memory(args)
    if args.cmd == "nightly":
        return _nightly(reg, args)
    if args.cmd == "relay-live":
        return _relay_live(args.vortex_url, args.ledger, args.interval, args.once, args.log_file)

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


def _status(reg: Registry, root: Path, ledger_path: str | None, as_json: bool) -> int:
    import os

    from linx import status

    ledger: Any = None
    source = "none"
    try:
        if ledger_path:
            from lestinxor.unisync import Ledger

            ledger = Ledger.from_jsonl(Path(ledger_path).read_text(encoding="utf-8"))
            source = ledger_path
        elif os.environ.get("UNISYNC_SUPABASE_URL"):
            from lestinxor.unisync_supabase import SupabaseLedger

            remote = SupabaseLedger()
            remote.sync()
            ledger, source = remote.ledger, "supabase"
    except ImportError:
        print("linx: install `linx[unisync]` to read the ledger", file=sys.stderr)
        return 2
    except Exception as exc:  # unreadable file, tampered row (ProtocolError), network: report, don't trace
        print(f"linx: cannot read ledger: {exc}", file=sys.stderr)
        return 2
    fleet = status.build(reg, root, ledger, source)
    print(json.dumps(fleet.to_dict(), indent=2) if as_json else status.render(fleet))
    return 1 if fleet.verified is False else 0


def _selftest(reg: Registry, root: Path, vortex_url: str | None) -> int:
    import os

    from linx import selftest

    env = os.environ.get
    steps = selftest.run(
        reg,
        root,
        vortex_url or env("LINX_VORTEX_URL", ""),
        env("VORTEX_OPERATOR_TOKEN", ""),
        env("UNISYNC_SUPABASE_URL", ""),
        env("UNISYNC_SUPABASE_KEY", ""),
    )
    print(selftest.render(steps))
    return 1 if any(s.result == selftest.FAIL for s in steps) else 0


def _split_ref(ref: str) -> tuple[str, str]:
    scope, _, key = ref.rpartition("/")
    return (scope or "shared"), key


def _open_ledger(path: str | None) -> tuple[Any, str]:
    """A postable ledger (`FileLedger` or `SupabaseLedger`, both expose `.ledger`) and its source name."""
    from linx import live

    if path:
        return live.FileLedger(path), path
    from lestinxor.unisync_supabase import SupabaseLedger

    remote = SupabaseLedger()
    remote.sync()
    return remote, "supabase"


def _memory(args: argparse.Namespace) -> int:
    from linx import memory

    try:
        led, _source = _open_ledger(args.ledger)
    except ImportError:
        print("linx: install `linx[unisync]` to use memory", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"linx: cannot open ledger: {exc}", file=sys.stderr)
        return 1 if memory.is_transient(exc) else 2  # 1 = retry later, 2 = fix the config
    view = led.ledger

    if args.mcmd in ("get", "history"):
        scope, key = _split_ref(args.ref)
        fact = memory.index(view.entries).get(f"{scope}/{key}")
        if fact is None:
            print(f"linx: no memory at {scope}/{key}", file=sys.stderr)
            return 1
        rows = fact.history if args.mcmd == "history" else [fact.current]
        for w in rows:
            print(f"#{w.seq} {w.sender}: {json.dumps(w.value, default=str)}")
        return 0

    if args.mcmd == "put":
        try:
            value: Any = json.loads(args.value)
        except json.JSONDecodeError:
            value = args.value
        try:
            env = led.post(args.sender, "memory.write", key=args.key, value=value, scope=args.scope)
        except Exception as exc:  # ProtocolError: unknown agent, etc.
            print(f"linx: {exc}", file=sys.stderr)
            return 2
        print(f"#{env.seq} {args.sender} wrote {args.scope}/{args.key}")
        return 0

    if args.mcmd == "check":
        report = memory.consolidate(view.entries, stale_after=args.stale_after)
        print(json.dumps(report.to_dict(), indent=2, default=str) if args.json else _render_memory(report))
        return 0

    if args.mcmd == "cards":
        try:
            paths = memory.write_cards(view.entries, args.out)
        except ValueError as exc:
            print(f"linx: {exc}", file=sys.stderr)
            return 2
        print(f"wrote {len(paths)} card(s) to {args.out}")
        return 0

    import logging

    fmt = "%(asctime)s %(levelname)s %(message)s"
    if getattr(args, "log_file", None):
        logging.basicConfig(
            level=logging.INFO, format=fmt, filename=args.log_file, encoding="utf-8", force=True
        )
    elif args.every:
        logging.basicConfig(level=logging.INFO, format=fmt)
    try:
        if args.every:
            memory.run(led, args.sender, stale_after=args.stale_after, every=args.every)
            return 0
        report = memory.consolidate(led.ledger.entries, stale_after=args.stale_after)
        n = memory.post(led, args.sender, report)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger("linx.memory").error("%s", exc)
        print(f"linx: {exc}", file=sys.stderr)
        return 2
    print(_render_memory(report) + f"\nposted {n} envelope(s) as {args.sender}")
    return 0


def _render_memory(report: Any) -> str:
    lines = [
        f"{report.facts} facts at ledger #{report.head}: {len(report.conflicts)} conflicts, "
        f"{len(report.stale)} stale, {len(report.duplicates)} duplicate groups, {len(report.links)} linked"
    ]
    for c in report.conflicts:
        lines.append(
            f"  conflict  {c.ref}: "
            + "; ".join(f"{s}={json.dumps(v, default=str)}" for s, v in c.values.items())
        )
    lines += [f"  stale     {r}" for r in report.stale]
    lines += [f"  duplicate {' = '.join(g)}" for g in report.duplicates]
    lines += [f"  link      {r} -> {', '.join(t)}" for r, t in report.links.items()]
    return "\n".join(lines)


def _nightly(reg: Registry, args: argparse.Namespace) -> int:
    from linx import memory, nightly

    try:
        led, _source = _open_ledger(args.ledger)
    except ImportError:
        print("linx: install `linx[unisync]` to use nightly", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"linx: cannot open ledger: {exc}", file=sys.stderr)
        return 1 if memory.is_transient(exc) else 2
    root = Path(args.root).resolve()
    day = args.date or nightly.today()
    try:
        if args.ncmd in ("plan", "run"):
            posted = nightly.plan(led, reg, root, day=day, sender=args.sender)
            print(f"planned {len(posted)} task(s)" + "".join(f"\n  {t}" for t in posted))
        if args.ncmd in ("work", "run"):
            agents = (
                [args.sender]
                if args.ncmd == "work"
                else sorted({s.nightly_agent for s in reg if s.nightly_agent and s.nightly_command})
            )
            for agent in agents:
                handled = nightly.work(led, reg, root, agent, timeout=args.timeout)
                print(f"{agent}: " + (f"ran {handled}" if handled else "nothing to do"))
        if args.ncmd in ("digest", "run"):
            report = memory.consolidate(led.ledger.entries)
            text = nightly.digest(led, day, memory_report=report)
            print(text, end="")
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
    except Exception as exc:  # ProtocolError (role/authority), unreadable ledger
        print(f"linx: {exc}", file=sys.stderr)
        return 2
    return 0


def _relay_live(
    vortex_url: str, ledger_path: str | None, interval: float, once: bool, log_file: str | None = None
) -> int:
    import logging
    import os

    from linx import live

    fmt = "%(asctime)s %(levelname)s %(message)s"
    if log_file:
        logging.basicConfig(level=logging.INFO, format=fmt, filename=log_file, encoding="utf-8", force=True)
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    log = logging.getLogger("linx.live")
    try:
        fetch = live.http_fetch(vortex_url, os.environ.get("VORTEX_OPERATOR_TOKEN", ""))
        ledger: Any
        if ledger_path:
            ledger = live.FileLedger(ledger_path)
        else:
            from lestinxor.unisync_supabase import SupabaseLedger

            ledger = SupabaseLedger()
        stats = live.run(ledger, fetch, interval=interval, max_polls=1 if once else None)
    except ImportError:
        log.error("install `linx[unisync]` to relay")
        print("linx: install `linx[unisync]` to relay", file=sys.stderr)
        return 2
    except (live.LiveError, ValueError) as exc:
        log.error("%s", exc)
        print(f"linx: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    summary = f"relayed {stats.relayed} event(s) in {stats.polls} poll(s); {stats.errors} error(s)"
    log.info("%s", summary)
    print(summary)
    return 1 if stats.errors and not stats.relayed and once else 0


if __name__ == "__main__":
    raise SystemExit(main())
