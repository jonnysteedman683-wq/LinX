"""Health of the whole fleet: which checkouts exist, and (on request) whether their checks pass."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from linx.registry import Registry, System

Runner = Callable[[Sequence[str], Path, float], int]


def _run(argv: Sequence[str], cwd: Path, timeout: float) -> int:
    try:
        return subprocess.run(
            list(argv), cwd=cwd, timeout=timeout, capture_output=True, check=False
        ).returncode
    except subprocess.TimeoutExpired:
        return 124


@dataclass(frozen=True)
class Report:
    system: str
    present: bool
    head: str | None
    missing_tools: tuple[str, ...]
    check_codes: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return self.present and not self.missing_tools and all(c == 0 for c in self.check_codes)


def _head(path: Path) -> str | None:
    head = path / ".git" / "HEAD"
    if not head.is_file():
        return None
    ref = head.read_text(encoding="utf-8").strip()
    if ref.startswith("ref: "):
        target = path / ".git" / ref[5:]
        return target.read_text(encoding="utf-8").strip()[:7] if target.is_file() else ref[16:]
    return ref[:7]


def inspect(
    system: System,
    root: Path,
    run_checks: bool = False,
    runner: Runner = _run,
    timeout: float = 900.0,
    which: Callable[[str], str | None] = shutil.which,
) -> Report:
    path = root / system.repo
    if not path.is_dir():
        return Report(system.name, False, None, ())
    tools = sorted({c[0] for c in system.checks})
    missing = tuple(t for t in tools if which(t) is None)
    codes: tuple[int, ...] = ()
    if run_checks and not missing:
        codes = tuple(runner(c, path, timeout) for c in system.checks)
    return Report(system.name, True, _head(path), missing, codes)


def doctor(
    registry: Registry,
    root: Path,
    run_checks: bool = False,
    runner: Runner = _run,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Report]:
    return [inspect(s, root, run_checks, runner, which=which) for s in registry]
