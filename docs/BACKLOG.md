# LinX backlog

Take the first unchecked item not marked 🔒. One item, one PR.

- [x] **X1** Registry (`systems.json`) of all nine systems, `linx systems`, `linx doctor`, `linx unisync`, `linx crosstest`.
- [x] **X2** Register `horizontal`, `vortex`, `neurocore` as UniSync agents: PR to Lestinxor's `DEFAULT_AGENTS` and `unisync/schema.sql` (SQL from `linx unisync`). *Done when* `linx unisync` prints "already".
- [x] **X3** Vortex ↔ UniSync relay: mirror Vortex task events (create/claim/complete/approve) into UniSync envelopes, read-only on Vortex. *Done when* a scripted Vortex event log replays into a verifying `Ledger`.
- [x] **X3b** `linx relay-live`: poll Vortex `/events`, relay to Supabase (or a JSONL file), resume from the ledger cursor, back off on network errors, stop on auth/data errors.
- [x] **X9** `linx selftest`: read-only PASS/FAIL check (UniSync agents, relay round-trip, checkouts, real Vortex, Supabase chain + agent registration), plus `Register-LinxRelayTask.ps1 -SelfTest`.
- [ ] **X4** Memory bridge: `memory.write` envelopes → HORIZONTAL-VAULT `02_Cards` markdown (dry-run output only; HORIZONTAL writes the vault). *Done when* golden-file test passes.
- [x] **X5** `linx status`: one table joining doctor + UniSync (open tasks, pending approvals per system).
- [ ] **X6** Neurocore intents → UniSync `task.request` via Hermes, always `requires_approval: true`.
- [x] **X8** Host the live relay: owner chose the same machine as Vortex. `Register-LinxRelayTask.ps1`, `START_LINX_RELAY.bat`, `scripts/windows/Start-LinxRelay.ps1`, `relay-live --log-file`, `python -m linx`.
- [ ] 🔒 **X7** CI workflow (needs owner decision: free Actions minutes only).

## Proposed
- Sentinel reads the UniSync ledger as its knowledge-graph source.
- HIVE-MIND-SWARM-OS public status page fed by `linx status --json`.
