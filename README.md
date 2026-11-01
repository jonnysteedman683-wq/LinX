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

### Live relay

`linx relay-live` polls Vortex's operator feed (`GET /events?after=N&once=1`) and relays new events
as they land. By default it writes to the shared Supabase ledger.

```bash
export VORTEX_OPERATOR_TOKEN=...                 # an operator's bearer token; never pass it as an argument
export UNISYNC_SUPABASE_URL=https://<ref>.supabase.co UNISYNC_SUPABASE_KEY=...   # service-role key
linx relay-live --vortex-url https://vortex.example            # runs until Ctrl-C, polls every 5s
linx relay-live --vortex-url http://127.0.0.1:8000 --ledger unisync.jsonl --once   # local, one poll
```

- It resumes from the ledger's own cursor, so a restart or a crash loses and duplicates nothing.
  If a second relay posts first, events are skipped instead of posted twice.
- Network failures and 5xx responses back off (5s, 10s, … up to 5 min) and retry. 401/403/404 and
  malformed events stop the relay with exit code 2: retrying can't fix them, and skipping would lose data.
- Plain `http` is accepted only for loopback, matching Vortex's own policy.
- Where to run it: next to Vortex (e.g. a scheduled task or service on the same machine, pointing at
  `http://127.0.0.1:8000`), or as a `--once` job on any free scheduler. Run one relay per ledger.

## Checks

```bash
python -m ruff check . && python -m ruff format --check . && python -m mypy && python -m pytest --cov=linx
```

Rules for agents working here: [`AGENTS.md`](AGENTS.md). Roadmap: [`docs/BACKLOG.md`](docs/BACKLOG.md).
