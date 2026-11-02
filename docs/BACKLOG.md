# LinX backlog

Take the first unchecked item not marked 🔒. One item, one PR.

- [x] **X1** Registry (`systems.json`) of all nine systems, `linx systems`, `linx doctor`, `linx unisync`, `linx crosstest`.
- [x] **X2** Register `horizontal`, `vortex`, `neurocore` as UniSync agents: PR to Lestinxor's `DEFAULT_AGENTS` and `unisync/schema.sql` (SQL from `linx unisync`). *Done when* `linx unisync` prints "already".
- [x] **X3** Vortex ↔ UniSync relay: mirror Vortex task events (create/claim/complete/approve) into UniSync envelopes, read-only on Vortex. *Done when* a scripted Vortex event log replays into a verifying `Ledger`.
- [x] **X3b** `linx relay-live`: poll Vortex `/events`, relay to Supabase (or a JSONL file), resume from the ledger cursor, back off on network errors, stop on auth/data errors.
- [x] **X9** `linx selftest`: read-only PASS/FAIL check (UniSync agents, relay round-trip, checkouts, real Vortex, Supabase chain + agent registration), plus `Register-LinxRelayTask.ps1 -SelfTest`.
- [x] **X4** Memory bridge + active memory (stage 1 of the layered memory): `linx memory get/history/put/check/consolidate/cards`. Conflicts, stale, duplicates, links; summary + one escalation per conflict, idempotent; HORIZONTAL-VAULT cards to a staging folder.
- [x] **X11** Memory loop at logon: `LinX-Memory` task (`Start-LinxService.ps1 -Service memory`), `-RunMemory`, resilient `memory consolidate --every` (backs off on network/5xx, exits 2 only on config/protocol errors), `--log-file`.
- [x] **X12** Nightly self-maintaining loop: `linx nightly plan/work/digest/run`; registry `nightly.agent`/`command`; Windows `LinX-Nightly` daily task + `-RunNightly`.
- [x] **X10** (Sentinel#134, merged) Active memory stage 2: Sentinel reads the UniSync ledger (Supabase, read-only) and renders facts, links and conflicts as its knowledge graph. PR in Sentinel; LinX exposes `linx memory check --json` as the contract.
- [x] **X5** `linx status`: one table joining doctor + UniSync (open tasks, pending approvals per system).
- [x] **X13** Phone digest: `linx notify` + automatic push after `nightly run` via ntfy (`LINX_NTFY_URL`/`_TOKEN`); owner approvals `linx pending/approve/reject`.
- [x] **X14** Kill switch: `linx halt/resume` (owner-only control scope); nightly refuses, memory loop pauses, status/digest banner.
- [x] **X15** Nightly retry cap: park after 3 failures + one escalation, `linx nightly retry`; failure feedback to the next attempt.
- [ ] **X16** Sebastian reads `LINX_FEEDBACK` in `evolve` (PR to Sebastian; `evolve.py` is guarded, so 🔒 owner-approved).
- [x] **X17** `linx ask`: English -> one allowlisted LinX command (rules, then optional local model via OpenAI-compatible API), parser-validated, always confirmed; owner actions need `yes`.
- [ ] **X18** `linx ask --explain`: repo expert. Stdlib BM25 index over the 9 repos' READMEs, AGENTS.md, backlogs and docs; answers cite the file; uses the local model when set.
- [ ] **X19** `linx listen`: push-to-talk voice in (local speech-to-text, optional `linx[voice]` extra), optional spoken reply; Windows first.
- [ ] **X6** Neurocore intents → UniSync `task.request` via Hermes, always `requires_approval: true`.
- [x] **X8** Host the live relay: owner chose the same machine as Vortex. `Register-LinxRelayTask.ps1`, `START_LINX_RELAY.bat`, `scripts/windows/Start-LinxService.ps1`, `relay-live --log-file`, `python -m linx`.
- [ ] 🔒 **X7** CI workflow (needs owner decision: free Actions minutes only).

## Proposed
- Sentinel reads the UniSync ledger as its knowledge-graph source.
- HIVE-MIND-SWARM-OS public status page fed by `linx status --json`.
