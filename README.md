# LinX

One registry, one bus and one control CLI over Jonny's agent systems. LinX doesn't replace any repo:
it links them. Pure Python, zero runtime dependencies.

```
                         Jonny (owner: approves, merges)
                                     │
  ┌─ orchestration ─────────────────────────────────────────────────────┐
  │  Lestinxor (thought search)          O0 (constitution + ledger)     │
  └───────────────┬─────────────────────────────────┬───────────────────┘
                  │        UniSync ledger (bus)     │   Vortex-MCP (live MCP
                  │  append-only, hash-chained      │   queue, leases, approvals)
  ┌─ execution ───┴─────────────────────────────────┴───────────────────┐
  │  Sebastian (sandboxed worker)        HORIZONTAL (evolution foundry) │
  └───────────────┬─────────────────────────────────────────────────────┘
  ┌─ memory ──────┴─────────────────────────────────────────────────────┐
  │  HORIZONTAL-VAULT (Obsidian)         Sentinel (knowledge graph UI)  │
  └─────────────────────────────────────────────────────────────────────┘
  ┌─ interface ─────────────────────────────────────────────────────────┐
  │  neurocore (BCI intents → Hermes)    HIVE-MIND-SWARM-OS (Wix web)   │
  └─────────────────────────────────────────────────────────────────────┘
```

| Piece | Role in LinX |
|---|---|
| `src/linx/systems.json` | The single source of truth: every system's repo, layer, UniSync role and check commands. |
| UniSync (from Lestinxor) | The bus. LinX doesn't fork it; `linx[unisync]` imports the reference implementation. |
| Sebastian `crosstest` | Cross-agent testing. `linx crosstest` writes its manifest from the registry. |

## Use

Clone LinX next to the other repos (same parent folder), then:

```bash
pip install -e ".[dev]"           # add ,unisync to read the live agent list from Lestinxor
linx systems                      # every system, by layer
linx doctor                       # which checkouts exist, and at which commit
linx doctor --checks              # also run every system's own checks
linx unisync                      # SQL that registers missing systems on the UniSync ledger
linx crosstest > agents.json      # then: sebastian crosstest --manifest agents.json --dry-run
linx relay events.json --ledger unisync.jsonl   # mirror a Vortex event feed onto UniSync
```

### Vortex → UniSync relay

`linx relay` takes the JSON array returned by Vortex's `vortex_operator_events` and posts it to a
UniSync ledger as the `vortex` watcher. Each event becomes a `memory.write` (scope `vortex`, key
`task/<id>`) holding that task's latest state; a `blocked` event also posts an `escalate`. Vortex
tasks are never re-dispatched as UniSync tasks. The cursor is read back from the ledger, so running
it twice appends nothing. Malformed events abort the whole batch before anything is posted.

## Checks

```bash
python -m ruff check . && python -m ruff format --check . && python -m mypy && python -m pytest --cov=linx
```

Rules for agents working here: [`AGENTS.md`](AGENTS.md). Roadmap: [`docs/BACKLOG.md`](docs/BACKLOG.md).
