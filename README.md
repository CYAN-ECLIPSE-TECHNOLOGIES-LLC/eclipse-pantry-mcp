# Eclipse Pantry MCP

**Powered by Cyan Eclipse Technologies** — https://cetech.ai

**Eclipse Pantry — the unattended-retail module: it reads vending fleets' own telemetry, predicts stock-outs, and plans the route before the truck rolls. It never touches card data.**

## Why this exists

A vending route runs on telemetry the machines already produce — DEX audit exports carry per-selection sales, cash counters, and fault events — but the data usually sits in a platform dashboard until someone opens it, often after a machine has sat empty for days. Pantry C0 is the read layer that fixes the first half of that: a local, governed MCP surface over the fleet's own DEX exports, where every number carries its provenance and every read leaves a receipt.

## Status — honest labels

v0.1.0, phase **C0 only (read-only telemetry, Tier 0)**. Local stdio transport, Python stdlib only, **no network calls anywhere in the code**, and the shipped fixtures are **synthetic** (fictional locations, invented serials). **No certification claimed — bench pending.**

C0 is the first phase of the Commercial Environment addendum (`~/workspace/cyan-eclipse/eclipse-commercial-environment-addendum-20261025.md` §3, §5). Explicitly NOT in v0.1, by spec: price changes, planogram changes (Tier 2, phase C2), route plans and pre-kitting manifests (Tier 1, phase C1), fault-ticket drafts (C1), and restock ordering — the closed loop runs through Eclipse Procure under Agent Wallet policy; Pantry supplies the forecast that starts it and the DEX read that closes it, nothing more.

## Tools (6 — Tier 0 only)

| Tool | What it does |
|---|---|
| `dex_ingest` | Parse a DEX/UCS-style audit export from a **local file** into a normalized read snapshot. **Fail-closed:** malformed content, a PAN-shaped value, an unenrolled machine, or a serial mismatch rejects the whole ingest with the reason (and line) named. Rejections are receipted too. |
| `inventory_read` | Product-level stock per enrolled machine from its latest read — counts, capacity, fill %, read age, and a **stale flag (>48h)**. A stale read is labeled, never silently freshened. |
| `sales_summary` | Vends, gross, and the cash vs cashless split from DEX counters — cumulative since counter reset by default, or period deltas between the reads bracketing `since`/`until`. Aggregates only; no card data exists in this system. |
| `stockout_forecast` | Days-to-empty per selection from measured sales velocity (delta between the two latest reads, else cumulative since reset) — a **labeled heuristic**, with stocked-out and at-risk (≤ horizon, default 7 days) flags. |
| `fault_read` | Fault/event records from each machine's latest DEX read, with labels and severity. Reports only — C0 drafts no tickets and dispatches nobody. |
| `receipt_list` | The append-only, hash-chained receipt ledger, with chain validity. The audit reader; itself not receipted. |

## The Machine Registry

Only enrolled machines exist in the tool surface — the Device Registry rule from the Procure Environment module, applied to fleets. Enrollment is an **operator file edit** (`registry.json` beside the server: machine id, type, location label, platform source, DEX serial), never an agent tool. A machine with no telemetry, or no registry entry, is invisible to Pantry — not estimated, not guessed. An ingest whose machine id is unenrolled, or whose DEX serial does not match the registry, is rejected and receipted.

## PCI law (encoded + tested)

**Card data lives with the reader processors. Pantry works in tokens and aggregates only; no PAN surface exists anywhere in the tool design.** The ingest schema has no card-number-shaped field, and the parser scans every file *before* parsing: a Luhn-valid 13–19-digit sequence anywhere in the file rejects the ingest with a named `PAN_DETECTED` error. The value itself is never stored, logged, or echoed — only its line number is recorded. A unit test proves the PAN appears nowhere in the state file or the error after a rejected ingest.

## DEX format

A DEX/UCS-style subset — asterisk-delimited records, one per line: `DXS` header (serial, audit counter), `ID1` machine identity, `DT1` collection time, `RS1` counter-reset time, `PA1` product assignments (selection, name, price), `VA2` sales since reset, `IN1` inventory snapshot (standing in for telemeter/platform inventory reads — raw DEX counters alone don't carry current counts), `CA3` cash totals (cash / cashless / cashless count), `EA1` event & fault records, `SE1`/`DXE` trailer. Validation is strict: unknown records, wrong field counts, non-integer counters, a sales record with no product assignment, inventory above capacity, or a missing trailer each reject the file at the offending line.

## Receipts

Every ingest — accepted, duplicate, or rejected — and every read verdict appends a **hash-chained receipt** to the store, append-only and machine-verifiable (`verify_receipt_chain`; tamper detection is unit-tested). The audit trail covers what was *attempted*, not just what landed.

## Data & IP

Fleet exhaust — per-machine, per-selection sales velocity, stock-out events, fault histories — is the **operator's own first-party data (rank (a)** under the Procure Data Rights Ledger). Pantry never pools one operator's location data into another operator's product, benchmarks, or models absent an explicit written agreement.

## Run it

```bash
python3 server.py            # MCP over stdio, stdlib only, no dependencies
python3 smoke.py             # end-to-end smoke over real stdio (temp state)
python3 -m unittest discover tests   # 14 tests
```

State persists in `data/state.json` beside the server (override with `ECLIPSE_PANTRY_STATE`); the registry is `registry.json` beside the server (override with `ECLIPSE_PANTRY_REGISTRY`). `data/` is gitignored — a real fleet's telemetry never belongs in a repo.

### Fixtures

`fixtures/` ships three synthetic DEX exports — a drink machine, a snack machine, and a combo machine, each with sales by selection, cash totals, inventory counts, and fault events, with totals a test verifies by hand-computed sums — plus two rejection fixtures: `dex-malformed.txt` (a non-numeric counter at line 7, an unknown record behind it) and `dex-with-pan.txt` (a card number pasted into a note record — the ingest must refuse it before parsing), and the `registry.json` that enrolls the three machines.

## Roadmap (per addendum §5)

- **C1** — Pantry planning: `route_plan`, `prekitting_manifest`, `fault_watch` ticket *drafts* (Tier 1 — documents; dispatch stays the operator's act). Porter pilot runs in the same phase, property-gated.
- **C2** — `price_change_prep` / `planogram_prep` (Tier 2: prepared batches, executed through the platform only after operator approval), per operator policy.

## License / attribution

A Cyan Eclipse Technologies product. The "Powered by Cyan Eclipse Technologies" attribution travels with it.
