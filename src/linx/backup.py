"""Backups, restore and anchors for the UniSync ledger (hardening 6).

Owner signatures stop forged rows, but someone with database access can still *delete* the newest
rows or replace the whole table with an older copy: the chain left behind still verifies. Two
defences, both stored off the database:

* **Backups**: `linx backup run` writes the whole ledger as JSONL plus a manifest (count, head,
  SHA-256). `verify` re-checks the file, its hash and the full chain (owner signatures included);
  `restore` writes a verified copy to a local ledger file, or into an *empty* Supabase table.
* **Anchors**: every backup appends `{seq, digest}` of the head to an anchor log on this machine.
  `linx backup check` replays the ledger against every anchor: a missing seq means rollback or
  deletion, a different digest means the history was rewritten. Either is exit 3 and, with ntfy set
  up, an urgent push. An anchor is only recorded when every earlier anchor still holds.

The anchor log lives at `LINX_ANCHORS`, else `%LOCALAPPDATA%\\LinX\\anchors.jsonl`, else
`~/.config/linx/anchors.jsonl`; backups default to a `backups` folder beside it. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = "linx-backup/1"
BATCH = 500


class BackupError(ValueError):
    """A backup file is missing, damaged, or does not match its manifest."""


@dataclass(frozen=True)
class Anchor:
    seq: int
    digest: str
    ts: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "digest": self.digest, "ts": self.ts, "source": self.source}


def _base() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "LinX" if base else Path.home() / ".config" / "linx"


def anchors_path() -> Path:
    return Path(os.environ["LINX_ANCHORS"]) if os.environ.get("LINX_ANCHORS") else _base() / "anchors.jsonl"


def backup_dir() -> Path:
    return Path(os.environ["LINX_BACKUP_DIR"]) if os.environ.get("LINX_BACKUP_DIR") else _base() / "backups"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ---- anchors ---------------------------------------------------------------------------------------


def load_anchors(path: Path | None = None) -> list[Anchor]:
    path = path or anchors_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BackupError(f"{path} is not UTF-8 text: {exc}") from exc
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            out.append(Anchor(int(raw["seq"]), str(raw["digest"]), float(raw["ts"]), str(raw["source"])))
        except (ValueError, KeyError, TypeError) as exc:
            raise BackupError(f"{path}:{n}: bad anchor line ({exc})") from exc
    return out


def check(entries: list[Any], anchors: list[Anchor], source: str | None = None) -> list[str]:
    """Problems found replaying `entries` against `anchors` (only those for `source`, if given)."""
    problems = []
    for a in anchors:
        if source is not None and a.source != source:
            continue
        if a.seq >= len(entries):
            problems.append(
                f"rollback: anchor saw #{a.seq} ({a.digest[:12]}...) but the ledger now ends at "
                f"#{len(entries) - 1}: rows were deleted or an older copy was restored"
            )
        elif entries[a.seq].digest != a.digest:
            problems.append(
                f"rewrite: #{a.seq} was {a.digest[:12]}... when anchored, "
                f"now {entries[a.seq].digest[:12]}...: history was replaced"
            )
    return problems


def record(entries: list[Any], source: str, *, path: Path | None = None, now: float | None = None) -> Anchor:
    """Append the current head as an anchor. Refuses when an earlier anchor no longer holds."""
    path = path or anchors_path()
    if not entries:
        raise BackupError("the ledger is empty: nothing to anchor")
    problems = check(entries, load_anchors(path), source)
    if problems:
        raise BackupError("; ".join(problems))
    head = entries[-1]
    anchor = Anchor(head.seq, head.digest, time.time() if now is None else now, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(anchor.to_dict(), sort_keys=True) + "\n")
    return anchor


# ---- backups ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Backup:
    path: Path
    manifest: dict[str, Any]


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def write(ledger: Any, source: str, *, out: Path | None = None, now: float | None = None) -> Backup:
    """Write `ledger` (a verified Lestinxor `Ledger`) as `unisync-<time>-<seq>.jsonl` + `.json` manifest."""
    entries = ledger.entries
    if not entries:
        raise BackupError("the ledger is empty: nothing to back up")
    now = time.time() if now is None else now
    out = out or backup_dir()
    out.mkdir(parents=True, exist_ok=True)
    text = ledger.to_jsonl()
    head = entries[-1]
    name = f"unisync-{_stamp(now)}-{head.seq}"
    manifest = {
        "format": FORMAT,
        "source": source,
        "created": now,
        "count": len(entries),
        "head_seq": head.seq,
        "head": head.digest,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    data = out / f"{name}.jsonl"
    _write_atomic(data, text)
    _write_atomic(out / f"{name}.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return Backup(data, manifest)


def listing(out: Path | None = None) -> list[Backup]:
    """Backups in `out`, oldest first (only those with a manifest)."""
    out = out or backup_dir()
    found = []
    for data in sorted(out.glob("unisync-*.jsonl")) if out.exists() else []:
        meta = data.with_suffix(".json")
        if meta.exists():
            try:
                found.append(Backup(data, json.loads(meta.read_text(encoding="utf-8"))))
            except ValueError:
                continue
    return found


def prune(keep: int, out: Path | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups. Returns the data files removed."""
    if keep < 1:
        raise BackupError("keep at least one backup")
    removed = []
    for b in listing(out)[:-keep]:
        b.path.unlink()
        b.path.with_suffix(".json").unlink(missing_ok=True)
        removed.append(b.path)
    return removed


def verify(path: Path, **owner_kwargs: Any) -> Any:
    """Re-check a backup: manifest, SHA-256, count, head and the whole chain. Returns the Ledger."""
    from lestinxor.unisync import Ledger, ProtocolError

    meta_path = path.with_suffix(".json")
    if not path.exists():
        raise BackupError(f"no backup at {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BackupError(f"{path} is not UTF-8 text") from exc
    manifest: dict[str, Any] | None = None
    if meta_path.exists():
        try:
            manifest = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise BackupError(f"manifest {meta_path} is not JSON") from exc
        if not isinstance(manifest, dict):
            raise BackupError(f"manifest {meta_path} is not a JSON object")
        if manifest.get("format") != FORMAT:
            raise BackupError(f"unknown backup format {manifest.get('format')!r}")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != manifest.get("sha256"):
            raise BackupError("SHA-256 does not match the manifest: the file was changed or truncated")
    try:
        ledger = Ledger.from_jsonl(text, **owner_kwargs)
    except (ProtocolError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # lestinxor raises ProtocolError for rule breaks; malformed rows can surface as the others
        raise BackupError(f"the chain does not verify: {type(exc).__name__}: {exc}") from exc
    if not ledger.entries:
        raise BackupError("the backup is empty")
    if manifest is not None:
        head = ledger.entries[-1]
        if (len(ledger.entries), head.seq, head.digest) != (
            manifest.get("count"),
            manifest.get("head_seq"),
            manifest.get("head"),
        ):
            raise BackupError("count or head does not match the manifest")
    return ledger


def restore_file(path: Path, to: Path, *, force: bool = False, **owner_kwargs: Any) -> Any:
    """Verify `path`, then write it as a local ledger at `to` (refuses to overwrite unless `force`)."""
    ledger = verify(path, **owner_kwargs)
    if to.exists() and to.stat().st_size and not force:
        raise BackupError(f"{to} already exists; pass --force to replace it")
    to.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(to, ledger.to_jsonl())
    return ledger


Opener = Callable[..., Any]


def restore_supabase(
    path: Path, url: str, key: str, *, opener: Opener | None = None, **owner_kwargs: Any
) -> Any:
    """Verify `path`, then insert its rows into an **empty** `unisync_events` table, in order.

    Refuses when the table already has rows: restoring over live history would fork the ledger.
    """
    ledger = verify(path, **owner_kwargs)
    if not url or not key:
        raise BackupError("set UNISYNC_SUPABASE_URL and UNISYNC_SUPABASE_KEY")
    if not url.startswith("https://"):
        raise BackupError("the Supabase URL must be https")
    endpoint = url.rstrip("/") + "/rest/v1/unisync_events"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    open_ = opener or urllib.request.urlopen

    req = urllib.request.Request(f"{endpoint}?select=seq&limit=1", headers=headers, method="GET")
    with open_(req, timeout=30) as resp:
        if json.loads(resp.read() or b"[]"):
            raise BackupError("the Supabase table is not empty: restore only into an empty unisync_events")
    rows = [e.to_dict() for e in ledger.entries]
    for i in range(0, len(rows), BATCH):
        body = json.dumps(rows[i : i + BATCH], default=str).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, headers={**headers, "Prefer": "return=minimal"}, method="POST"
        )
        with open_(req, timeout=60) as resp:
            resp.read()
    return ledger
