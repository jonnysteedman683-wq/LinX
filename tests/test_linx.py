from __future__ import annotations

import json
from pathlib import Path

import pytest

from linx import LAYERS, load
from linx.bridges import crosstest_manifest, unisync_gaps, unisync_sql
from linx.cli import main
from linx.doctor import doctor
from linx.registry import Registry, RegistryError

UNISYNC_TODAY = {
    "jonny",
    "lestinxor",
    "sebastian",
    "jules",
    "claude",
    "hermes",
    "o0",
    "grok",
    "coordinator-bot",
}


def test_bundled_registry_covers_all_nine_systems() -> None:
    reg = load()
    assert len(reg) == 9
    assert {s.layer for s in reg} == set(LAYERS)
    assert reg.get("sebastian").slug(reg.owner) == "jonnysteedman683-wq/Sebastian"


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.update(version=2), "version"),
        (lambda r: r.pop("owner"), "owner"),
        (lambda r: r["systems"][0].update(layer="cloud"), "layer"),
        (lambda r: r["systems"][0].update(role="king"), "role"),
        (lambda r: r["systems"][0].update(checks=[[]]), "argv"),
        (lambda r: r["systems"][0].pop("repo"), "repo"),
        (lambda r: r["systems"].append(dict(r["systems"][0])), "duplicate"),
    ],
)
def test_registry_rejects_bad_input(mutate, msg) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads((Path(__file__).parents[1] / "src/linx/systems.json").read_text())
    mutate(raw)
    with pytest.raises(RegistryError, match=msg):
        Registry.from_dict(raw)


def test_unisync_gaps_and_sql() -> None:
    reg = load()
    assert unisync_gaps(reg, UNISYNC_TODAY) == ["horizontal", "neurocore", "vortex"]
    sql = unisync_sql(reg, UNISYNC_TODAY)
    assert "('vortex', 'watcher', '{jonnysteedman683-wq/Vortex-MCP}')" in sql
    assert sql.rstrip().endswith("on conflict (name) do nothing;")
    everyone = UNISYNC_TODAY | {"horizontal", "neurocore", "vortex"}
    assert unisync_sql(reg, everyone).startswith("-- every")


def test_crosstest_manifest_only_lists_known_commands() -> None:
    m = crosstest_manifest(load(), {"sebastian": ["sebastian", "run", "{task}"]})
    assert m == {
        "version": 1,
        "agents": [
            {
                "name": "sebastian",
                "suite": "Sebastian/evals/crosstest",
                "command": ["sebastian", "run", "{task}"],
                "timeout": 600,
            }
        ],
    }


def test_doctor_reports_presence_tools_and_checks(tmp_path: Path) -> None:
    reg = load()
    (tmp_path / "Sebastian" / ".git" / "refs" / "heads").mkdir(parents=True)
    (tmp_path / "Sebastian" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "Sebastian" / ".git" / "refs" / "heads" / "main").write_text("abcdef1234\n")
    (tmp_path / "neurocore").mkdir()
    calls: list[tuple[str, ...]] = []

    def runner(argv, cwd, timeout):  # type: ignore[no-untyped-def]
        calls.append(tuple(argv))
        return 0 if cwd.name == "Sebastian" else 1

    reports = {
        r.system: r
        for r in doctor(
            reg, tmp_path, run_checks=True, runner=runner, which=lambda t: None if t == "pnpm" else t
        )
    }
    assert reports["sebastian"].ok and reports["sebastian"].head == "abcdef1"
    assert not reports["o0"].present
    assert reports["neurocore"].missing_tools == ("pnpm",) and not reports["neurocore"].ok
    assert calls == [("python", "-m", "pytest", "-q")]


def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["systems"]) == 0
    assert "orchestration:" in capsys.readouterr().out
    assert main(["unisync", "--registered", ",".join(UNISYNC_TODAY)]) == 0
    assert "'horizontal'" in capsys.readouterr().out
    assert main(["crosstest"]) == 0
    assert json.loads(capsys.readouterr().out)["agents"][0]["name"] == "sebastian"
    assert main(["doctor", "--root", str(tmp_path)]) == 1
    assert "MISSING" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert main(["--registry", str(bad), "systems"]) == 2
