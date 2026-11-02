"""The owner key: Jonny's signature on approvals, halts and every other owner action.

* `linx owner init` makes a secret seed (32 random bytes) in `%LOCALAPPDATA%\\LinX\\owner.seed` (or
  `~/.config/linx/owner.seed`, or `LINX_OWNER_SEED_FILE`) and prints the public owner key.
* The public key is pinned for every reader: `owner_key: {"root": ..., "from": N}` in `systems.json`, or
  `UNISYNC_OWNER_KEY` / `UNISYNC_OWNER_FROM` in the environment (environment wins). From ledger entry N
  on, owner entries without a valid signature are refused (Lestinxor's `owner_sig`).
* On the machine with the seed, LinX signs the owner's posts automatically and records the one-time keys
  it used in `owner.state` next to the seed, so none is reused.

Without a pinned key nothing changes (no signing, no checks). Keep the seed secret and backed up: whoever
holds it can act as the owner, and losing it means rotating to a new key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from linx.registry import Registry


def seed_path() -> Path:
    if os.environ.get("LINX_OWNER_SEED_FILE"):
        return Path(os.environ["LINX_OWNER_SEED_FILE"])
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "LinX" if base else Path.home() / ".config" / "linx") / "owner.seed"


def pinned(registry: Registry | None) -> tuple[str | None, int]:
    """The owner key every reader should enforce, and the seq it applies from."""
    root = os.environ.get("UNISYNC_OWNER_KEY") or (registry.owner_key if registry else None)
    raw_from = os.environ.get("UNISYNC_OWNER_FROM")
    start = int(raw_from) if raw_from else (registry.owner_from if registry else 0)
    return (root or None), start


def load_key(path: Path | None = None, height: int = 10) -> Any:
    from lestinxor.owner_sig import OwnerKey

    path = path or seed_path()
    return OwnerKey(bytes.fromhex(path.read_text(encoding="utf-8").strip()), height)


def init(path: Path | None = None, *, force: bool = False, height: int = 10) -> str:
    """Create a new seed and return its public owner key. Refuses to overwrite one unless `force`."""
    from lestinxor.owner_sig import OwnerKey

    path = path or seed_path()
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; use --force only to rotate to a new owner key")
    key = OwnerKey.generate(height)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.seed.hex(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # owner-only on POSIX; on Windows the profile folder is already per-user
    except OSError:
        pass
    state = path.with_name("owner.state")
    if state.exists():
        state.unlink()  # a new key starts its one-time keys from zero
    return str(key.root)


def ledger_kwargs(registry: Registry | None, *, sign: bool = True) -> dict[str, Any]:
    """Arguments for lestinxor's Ledger/SupabaseLedger: enforce the pinned key, and sign if we hold it."""
    root, start = pinned(registry)
    if not root:
        return {}
    kwargs: dict[str, Any] = {"owner_root": root, "owner_from": start}
    path = seed_path()
    if sign and path.exists():
        kwargs["signer"] = _LazySigner(path, root)
    return kwargs


class _LazySigner:
    """Loads the seed and builds its key tree (~1 s) only when an owner entry is actually signed."""

    def __init__(self, path: Path, root: str) -> None:
        self.path = path
        self.root = root
        self._sign: Any = None

    def __call__(self, message: bytes, ledger_max_index: int) -> str:
        if self._sign is None:
            from lestinxor.owner_sig import OwnerSigner

            key = load_key(self.path)
            if key.root != self.root:
                raise ValueError(
                    f"the owner seed at {self.path} does not match the pinned owner key {self.root[:12]}..."
                )
            self._sign = OwnerSigner(key, self.path.with_name("owner.state")).sign
        return str(self._sign(message, ledger_max_index))


def status(registry: Registry | None) -> list[str]:
    root, start = pinned(registry)
    path = seed_path()
    lines = [f"pinned owner key: {root or '(none: owner entries are checked by name only)'}"]
    if root:
        lines.append(f"enforced from ledger entry: {start}")
    if path.exists():
        try:
            mine = load_key(path).root
            match = "matches" if root and mine == root else "DOES NOT match" if root else "not pinned yet"
            lines.append(f"seed: {path} (key {mine[:12]}..., {match})")
        except (OSError, ValueError) as exc:
            lines.append(f"seed: {path} unreadable: {exc}")
    else:
        lines.append(f"seed: none at {path} (this machine cannot sign owner actions)")
    return lines
