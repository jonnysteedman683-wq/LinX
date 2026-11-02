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
linx status                       # one view: checkouts + UniSync (Supabase if configured)
linx selftest                     # read-only PASS/FAIL check of the whole chain on this machine
linx memory check                 # shared memory: conflicts, stale facts, duplicates, links
linx status --ledger unisync.jsonl --json       # same, from a file, machine-readable
```

### Active memory

One shared memory for every system, on the UniSync ledger (the record of truth): every fact is a
`memory.write` by a registered agent, so each value has an author, a ledger position and a hash chain.

```bash
linx memory put build_cmd "python -m pytest" --as sebastian      # JSON if it parses, else text
linx memory get build_cmd                                        # '#8 sebastian: "python -m pytest"'
linx memory history vortex/task/t-aaa                            # every write, oldest first
linx memory check                                                # read-only report
linx memory consolidate --as coordinator-bot --every 300         # keep it active
linx memory cards --out ./staging/memory                         # Obsidian cards for HORIZONTAL-VAULT
```

`check` finds **conflicts** (two or more agents disagree on a key within its last 5 writes),
**stale** facts (last write 500+ entries behind the head; `--stale-after`), **duplicates** (keys in one
scope holding the same value) and **links** (a value naming another key as `scope/key`, or a bare key
in the same scope; a heuristic, so short common key names can link loosely).

`consolidate` makes it active: the agent you name posts the report as `linx/memory/consolidation`
and escalates each conflict once. Re-running with nothing new posts nothing. `cards` writes one card
per fact plus `000_UniSync_Memory_MOC.md`, in HORIZONTAL-VAULT's frontmatter format, with `[[links]]`
and conflict/stale tags. It refuses a folder that looks like a live vault; HORIZONTAL imports them.

Stage 2 (X10): Sentinel reads the same ledger and shows it as its live graph (Mind Map tab,
`GET /api/unisync/memory`, read-only).

### Selftest

```
[PASS] unisync    12 agents known, all LinX agents present
[PASS] relay      4 events relayed, replay verifies, re-run adds nothing, status reads it
[INFO] checkouts  9/9 under C:\Users\jonny\repos
[PASS] vortex     http://127.0.0.1:8000 answered with 7 event(s); 7 relayed into a throwaway ledger
[FAIL] supabase   12 entries verify, but agents not registered: horizontal, neurocore, vortex
                  fix: run Lestinxor's unisync/schema.sql in the Supabase SQL editor (safe to re-run)
```

Nothing is written anywhere: Vortex events go into a throwaway in-memory ledger, and Supabase is only
read. `vortex` uses `LINX_VORTEX_URL` (or `--vortex-url`) and `VORTEX_OPERATOR_TOKEN`; `supabase` uses
`UNISYNC_SUPABASE_URL`/`_KEY`. Each is skipped when its settings are absent. Exit 1 if anything failed.

### Status

```
system            layer          checkout  held open appr done fail  last
lestinxor         orchestration  565e6f2      0    0    0    0    0  -
vortex            transport      b8c5636      0    0    0    0    0  #4
sentinel          memory         481fd81      -    -    -    -    -  -
...
ledger: supabase, 5 entries, verified
pending approvals: 0
vortex tasks: blocked 1, open 1
recent escalations:
  #3 vortex: Vortex task fc64f054-… blocked: vault locked
```

`held` = tasks the agent has claimed, `open` = tasks it requested that are still in flight, `appr` =
its requests waiting on Jonny, `done`/`fail` = tasks it finished, `last` = its latest ledger entry.
Systems without a UniSync role show `-`. Reading the ledger re-verifies the whole hash chain; a
tampered or rule-breaking row exits 2 instead of showing numbers.

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
- Run one relay per ledger. `--log-file PATH` appends logs (UTF-8) instead of writing to stderr.

#### On Windows, next to Vortex: relay + memory at every logon

```powershell
pip install -e ".[unisync]"                 # from the LinX folder, into the python the task will use
.\Register-LinxRelayTask.ps1 -SetSecrets    # Vortex token, Supabase URL + key, memory agent/interval (once)
.\Register-LinxRelayTask.ps1 -SelfTest      # read-only: PASS/FAIL for UniSync, relay, Vortex, Supabase
.\Register-LinxRelayTask.ps1 -Run           # relay in this window; Ctrl+C when it says "relayed ..."
.\Register-LinxRelayTask.ps1 -RunMemory     # memory loop in this window; Ctrl+C after the first summary
.\Register-LinxRelayTask.ps1 -Register      # both start at every logon (Task Scheduler, or Startup folder)
.\Register-LinxRelayTask.ps1 -Status        # both tasks, last log lines, which settings are set
```

Two logon tasks run `scripts/windows/Start-LinxService.ps1`: `LinX-Relay` (`-Service relay`) and
`LinX-Memory` (`-Service memory`, i.e. `linx memory consolidate --every`). Logs:
`%LOCALAPPDATA%\LinX\relay.log` and `memory.log`. Each supervisor restarts its loop after a crash
(10 s, doubling to 10 min) but stops on exit code 2 (bad token, URL, unknown agent or bad data), so
a wrong setting never hammers anything. Inside the memory loop, network errors and 5xx/429 back off
and retry without exiting. Secrets are user environment variables, never files in the repo.
Optional: `LINX_VORTEX_URL` (default `http://127.0.0.1:8000`), `LINX_MEMORY_AGENT` (default
`coordinator-bot`), `LINX_MEMORY_EVERY` (default 300 s), `LINX_LEDGER` (local JSONL instead of
Supabase), `LINX_PYTHON`. `START_LINX_RELAY.bat` runs the relay in a console window.

## Checks

```bash
python -m ruff check . && python -m ruff format --check . && python -m mypy && python -m pytest --cov=linx
```

Rules for agents working here: [`AGENTS.md`](AGENTS.md). Roadmap: [`docs/BACKLOG.md`](docs/BACKLOG.md).
