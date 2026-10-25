#!/usr/bin/env python3
"""Eclipse Pantry MCP — v0.1.0, phase C0 (READ-ONLY telemetry).

Powered by Cyan Eclipse Technologies (cetech.ai).

Eclipse Pantry — the unattended-retail module: it reads vending fleets'
own telemetry, predicts stock-outs, and plans the route before the
truck rolls. It never touches card data. (v0.1 ships the C0 half of
that descriptor: the reads and the forecast. Route planning is C1.)

C0 SCOPE — TIER 0 ONLY:
  * dex_ingest: parse a DEX/UCS-style audit export from a LOCAL file
    into a normalized read snapshot. Fail-closed: any malformed line
    rejects the whole file with the line number named; nothing is
    stored. Every field of the snapshot carries provenance
    {machine_id, dex_serial, audit_counter, read_time, source_file}.
  * Machine Registry: only enrolled machines exist in the tool
    surface. A DEX export whose machine identity / serial does not
    match the registry is rejected. Unenrolled = invisible.
  * inventory_read / sales_summary / stockout_forecast / fault_read:
    reads over the stored snapshots. Nothing actuates, nothing is
    priced, no ticket is drafted, no order is placed.

OUT OF SCOPE in C0 (by spec, not by omission): price changes,
planogram writes, route plans, pre-kitting manifests, fault-ticket
drafts, restock ordering (the Procure loop), ANY network call. There
is deliberately no code path here that contacts a telemetry platform,
a processor, or the internet. Fixtures are synthetic.

PCI LAW (encoded, tested): card data lives with the reader
processors. The ingest schema has NO card-number-shaped field, and
the parser scans every file for PAN-shaped values BEFORE parsing:
a Luhn-valid 13-19 digit sequence anywhere in the file rejects the
ingest with a named PAN_DETECTED error. The rejected value is never
echoed into the error, the receipt, or the store — only its line
number is recorded.

Receipts: every ingest (accepted, duplicate, or rejected) and every
read verdict appends a hash-chained receipt — append-only,
verifiable. receipt_list itself is the audit reader and is not
receipted (it would recurse).

Transport: MCP over stdio (newline-delimited JSON-RPC 2.0), stdlib
only. State: $ECLIPSE_PANTRY_STATE or ./data/state.json beside this
file. Registry: $ECLIPSE_PANTRY_REGISTRY or ./registry.json beside
this file — enrollment is an operator file edit (the Device Registry
pattern), never an agent tool.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

STATE_PATH = Path(os.environ.get(
    "ECLIPSE_PANTRY_STATE",
    Path(__file__).parent / "data" / "state.json"))
REGISTRY_PATH = Path(os.environ.get(
    "ECLIPSE_PANTRY_REGISTRY",
    Path(__file__).parent / "registry.json"))

STALE_AFTER_HOURS = 48.0        # a telemetry read older than this is stale
FORECAST_HORIZON_DEFAULT = 7    # days; selections emptying sooner are at risk

EVENT_LABELS = {
    "SOLD_OUT": "Selection sold out",
    "TEMP_HIGH": "Cabinet temperature high",
    "JAM": "Vend mechanism jam",
    "COIN_JAM": "Coin mechanism jam",
    "BILL_VAL": "Bill validator fault",
    "MOTOR_FAIL": "Vend motor failure",
    "DOOR_OPEN": "Door opened",
}
EVENT_SEVERITY = {
    "TEMP_HIGH": "high", "MOTOR_FAIL": "high",
    "JAM": "medium", "COIN_JAM": "medium", "BILL_VAL": "medium",
    "SOLD_OUT": "low", "DOOR_OPEN": "low",
}


class ToolError(Exception):
    pass


class DexError(ToolError):
    """Fail-closed parse/validation error with a machine-readable code
    and a 1-based line reference (None when not line-specific)."""

    def __init__(self, code, message, line=None):
        super().__init__(message)
        self.code = code
        self.line = line


def now() -> int:
    return int(time.time())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------ receipts
def emit_receipt(state: dict, rtype: str, detail: dict) -> dict:
    """Append-only, hash-chained (house pattern)."""
    prev = state["receipts"][-1]["hash"] if state["receipts"] else "GENESIS"
    entry = {"seq": len(state["receipts"]) + 1, "ts": now(), "type": rtype,
             "prev_hash": prev, "detail": detail}
    entry["hash"] = sha(prev + canon({k: v for k, v in entry.items()
                                      if k != "hash"}))
    state["receipts"].append(entry)
    return entry


def verify_receipt_chain(receipts) -> bool:
    prev = "GENESIS"
    for e in receipts:
        if e.get("prev_hash") != prev:
            return False
        expect = sha(prev + canon({k: v for k, v in e.items() if k != "hash"}))
        if e.get("hash") != expect:
            return False
        prev = e["hash"]
    return True


# --------------------------------------------------------------------- state
def empty_state() -> dict:
    return {"reads": {}, "receipts": []}  # reads: machine_id -> [snapshot]


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return empty_state()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    doc = json.loads(REGISTRY_PATH.read_text())
    return {m["machine_id"]: m for m in doc.get("machines", [])}


# ----------------------------------------------------------------- PCI guard
_PAN_CANDIDATE = re.compile(r"(?:\d[ -]?){12,18}\d")


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def scan_for_pan(text: str):
    """Returns the 1-based line number of the first PAN-shaped value,
    or None. PAN-shaped = 13-19 digits (spaces/dashes allowed between
    digits) that passes the Luhn check. The value itself is NEVER
    returned, logged, or stored."""
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _PAN_CANDIDATE.finditer(line):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                return lineno
    return None


# ----------------------------------------------------------------- DEX parse
# DEX/UCS-style subset (asterisk-delimited records, one per line):
#   DXS*<serial>*<comm_id>*VA*V1.6*<audit_counter>      header (line 1)
#   ID1*<machine_id>*<machine_type>                      identity
#   DT1*<YYYYMMDD>*<HHMM>                               collection time
#   RS1*<YYYYMMDD>*<HHMM>                               counter-reset time
#   PA1*<selection>*<product_name>*<price_cents>         product assignment
#   VA2*<selection>*<vends>*<value_cents>*<free_vends>   sales since reset
#   IN1*<selection>*<inventory_count>*<capacity>         inventory snapshot
#   CA3*<cash_cents>*<cashless_cents>*<cashless_count>   cash totals
#   EA1*<event_code>*<YYYYMMDD>*<HHMM>*<occurrences>     event / fault
#   SE1*<record_count>                                   set end
#   DXE*1                                                trailer (last line)
# IN1 stands in for telemeter inventory snapshots (platform exports
# carry current counts; raw DEX counters alone do not).

def _int(field, lineno, what):
    try:
        v = int(field)
    except (TypeError, ValueError):
        raise DexError("DEX_PARSE_ERROR",
                       f"line {lineno}: {what} must be an integer, "
                       f"got {field!r}", line=lineno)
    if v < 0:
        raise DexError("DEX_PARSE_ERROR",
                       f"line {lineno}: {what} must be >= 0, got {v}",
                       line=lineno)
    return v


def _stamp(date_s, time_s, lineno):
    try:
        return datetime.strptime(date_s + time_s, "%Y%m%d%H%M")
    except ValueError:
        raise DexError("DEX_PARSE_ERROR",
                       f"line {lineno}: invalid date/time "
                       f"{date_s!r} {time_s!r}", line=lineno)


def parse_dex(text: str, source: str) -> dict:
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise DexError("DEX_PARSE_ERROR", "empty file — no DEX records")
    snap = {"selections": {}, "events": [], "cash": None,
            "provenance": {"source_file": source}}
    order = []
    for lineno, raw in enumerate(lines, 1):
        parts = raw.split("*")
        rec, fields = parts[0], parts[1:]
        order.append(rec)

        def need(n, what):
            if len(fields) != n:
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: {rec} expects {n} fields "
                               f"({what}), got {len(fields)}", line=lineno)

        if rec == "DXS":
            need(5, "serial, comm_id, VA, version, audit_counter")
            if lineno != 1:
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: DXS must be the first record",
                               line=lineno)
            snap["dex_serial"] = fields[0]
            snap["audit_counter"] = _int(fields[4], lineno, "audit counter")
            snap["provenance"]["dex_serial"] = fields[0]
            snap["provenance"]["audit_counter"] = snap["audit_counter"]
        elif rec == "ID1":
            need(2, "machine_id, machine_type")
            snap["machine_id"] = fields[0]
            snap["machine_type_declared"] = fields[1]
        elif rec == "DT1":
            need(2, "YYYYMMDD, HHMM")
            snap["read_at"] = _stamp(fields[0], fields[1], lineno)
        elif rec == "RS1":
            need(2, "YYYYMMDD, HHMM")
            snap["reset_at"] = _stamp(fields[0], fields[1], lineno)
        elif rec == "PA1":
            need(3, "selection, product_name, price_cents")
            sel = fields[0]
            if sel in snap["selections"]:
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: duplicate product assignment "
                               f"for selection {sel}", line=lineno)
            snap["selections"][sel] = {
                "selection": sel, "name": fields[1],
                "price_cents": _int(fields[2], lineno, "price")}
        elif rec == "VA2":
            need(4, "selection, vends, value_cents, free_vends")
            sel = fields[0]
            if sel not in snap["selections"]:
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: VA2 for selection {sel} "
                               f"with no PA1 assignment", line=lineno)
            snap["selections"][sel].update({
                "vends": _int(fields[1], lineno, "vends"),
                "value_cents": _int(fields[2], lineno, "value_cents"),
                "free_vends": _int(fields[3], lineno, "free_vends")})
        elif rec == "IN1":
            need(3, "selection, inventory_count, capacity")
            sel = fields[0]
            if sel not in snap["selections"]:
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: IN1 for selection {sel} "
                               f"with no PA1 assignment", line=lineno)
            inv = _int(fields[1], lineno, "inventory count")
            cap = _int(fields[2], lineno, "capacity")
            if inv > cap:
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: inventory {inv} exceeds "
                               f"capacity {cap} for selection {sel}",
                               line=lineno)
            snap["selections"][sel].update(
                {"inventory": inv, "capacity": cap})
        elif rec == "CA3":
            need(3, "cash_cents, cashless_cents, cashless_count")
            if snap["cash"] is not None:
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: duplicate CA3 record",
                               line=lineno)
            snap["cash"] = {
                "cash_cents": _int(fields[0], lineno, "cash_cents"),
                "cashless_cents": _int(fields[1], lineno, "cashless_cents"),
                "cashless_count": _int(fields[2], lineno, "cashless_count")}
        elif rec == "EA1":
            need(4, "event_code, YYYYMMDD, HHMM, occurrences")
            when = _stamp(fields[1], fields[2], lineno)
            snap["events"].append({
                "code": fields[0], "occurred_at": when.isoformat(),
                "occurrences": _int(fields[3], lineno, "occurrences")})
        elif rec == "SE1":
            need(1, "record_count")
            _int(fields[0], lineno, "record count")
        elif rec == "DXE":
            need(1, "trailer value")
            if lineno != len(lines):
                raise DexError("DEX_PARSE_ERROR",
                               f"line {lineno}: DXE must be the last record",
                               line=lineno)
        else:
            raise DexError("DEX_PARSE_ERROR",
                           f"line {lineno}: unknown record type {rec!r}",
                           line=lineno)

    if order[0] != "DXS" or order[-1] != "DXE":
        raise DexError("DEX_PARSE_ERROR",
                       "file must open with DXS and close with DXE")
    for required in ("machine_id", "read_at", "reset_at", "cash"):
        if required not in snap or snap[required] is None:
            raise DexError("DEX_PARSE_ERROR",
                           f"missing required record for {required}")
    if snap["read_at"] < snap["reset_at"]:
        raise DexError("DEX_PARSE_ERROR",
                       "collection time (DT1) precedes counter reset (RS1)")
    for sel, row in snap["selections"].items():
        for key in ("vends", "inventory"):
            if key not in row:
                raise DexError("DEX_PARSE_ERROR",
                               f"selection {sel} is missing its "
                               f"{'VA2 sales' if key == 'vends' else 'IN1 inventory'} "
                               f"record")
    snap["read_at"] = snap["read_at"].isoformat()
    snap["reset_at"] = snap["reset_at"].isoformat()
    snap["selections"] = [snap["selections"][k]
                          for k in sorted(snap["selections"])]
    return snap


# -------------------------------------------------------------------- ingest
def t_dex_ingest(state, args):
    path = Path(str(args["path"])).expanduser()
    if not path.exists():
        r = emit_receipt(state, "dex_ingest_rejected",
                         {"source_file": path.name,
                          "code": "FILE_NOT_FOUND"})
        return {"status": "rejected", "code": "FILE_NOT_FOUND",
                "error": f"file not found: {path}", "receipt_seq": r["seq"]}
    text = path.read_text()
    pan_line = scan_for_pan(text)
    if pan_line is not None:
        r = emit_receipt(state, "dex_ingest_rejected",
                         {"source_file": path.name, "code": "PAN_DETECTED",
                          "line": pan_line,
                          "note": "PAN-shaped value redacted — never stored"})
        return {"status": "rejected", "code": "PAN_DETECTED",
                "error": f"PAN_DETECTED: line {pan_line} contains a "
                         f"card-number-shaped value (Luhn-valid, 13-19 "
                         f"digits). Pantry's ingest schema has no field "
                         f"for card data; the value was not stored, "
                         f"logged, or echoed.",
                "receipt_seq": r["seq"]}
    try:
        snap = parse_dex(text, path.name)
    except DexError as e:
        r = emit_receipt(state, "dex_ingest_rejected",
                         {"source_file": path.name, "code": e.code,
                          "line": e.line})
        return {"status": "rejected", "code": e.code, "line": e.line,
                "error": str(e), "receipt_seq": r["seq"]}

    registry = load_registry()
    entry = registry.get(snap["machine_id"])
    if entry is None:
        r = emit_receipt(state, "dex_ingest_rejected",
                         {"source_file": path.name,
                          "code": "MACHINE_NOT_ENROLLED",
                          "machine_id": snap["machine_id"]})
        return {"status": "rejected", "code": "MACHINE_NOT_ENROLLED",
                "error": f"machine {snap['machine_id']} is not in the "
                         f"Machine Registry — unenrolled machines are "
                         f"invisible to Pantry. Enroll it in registry.json "
                         f"(operator action) before ingesting its telemetry.",
                "receipt_seq": r["seq"]}
    if entry.get("serial") != snap["dex_serial"]:
        r = emit_receipt(state, "dex_ingest_rejected",
                         {"source_file": path.name,
                          "code": "SERIAL_MISMATCH",
                          "machine_id": snap["machine_id"]})
        return {"status": "rejected", "code": "SERIAL_MISMATCH",
                "error": f"DEX serial for {snap['machine_id']} does not "
                         f"match the registry serial — refusing a read "
                         f"whose identity does not check out.",
                "receipt_seq": r["seq"]}

    snap["machine_type"] = entry["type"]
    snap["location"] = entry["location"]
    snap["platform_source"] = entry["platform_source"]
    reads = state["reads"].setdefault(snap["machine_id"], [])
    for old in reads:
        if (old["audit_counter"] == snap["audit_counter"]
                and old["read_at"] == snap["read_at"]):
            r = emit_receipt(state, "dex_ingest_duplicate",
                             {"machine_id": snap["machine_id"],
                              "audit_counter": snap["audit_counter"],
                              "read_at": snap["read_at"]})
            return {"status": "duplicate_ignored",
                    "machine_id": snap["machine_id"],
                    "note": "This read (audit counter + collection time) "
                            "is already stored; nothing changed.",
                    "receipt_seq": r["seq"]}
    reads.append(snap)
    reads.sort(key=lambda s: s["read_at"])
    totals = {"vends": sum(s["vends"] for s in snap["selections"]),
              "gross_cents": sum(s["value_cents"] for s in snap["selections"])}
    r = emit_receipt(state, "dex_ingest",
                     {"machine_id": snap["machine_id"],
                      "audit_counter": snap["audit_counter"],
                      "read_at": snap["read_at"],
                      "selections": len(snap["selections"]),
                      "vends_since_reset": totals["vends"]})
    return {"status": "ingested", "machine_id": snap["machine_id"],
            "type": snap["machine_type"], "location": snap["location"],
            "read_at": snap["read_at"], "reset_at": snap["reset_at"],
            "audit_counter": snap["audit_counter"],
            "selections": len(snap["selections"]), "totals": totals,
            "cash": snap["cash"], "events": len(snap["events"]),
            "provenance": snap["provenance"], "receipt_seq": r["seq"]}


# ---------------------------------------------------------------------- reads
def parse_as_of(args):
    raw = args.get("as_of")
    if not raw:
        return datetime.now()
    raw = str(raw)
    try:
        if len(raw) == 10:
            return datetime.combine(datetime.fromisoformat(raw).date(),
                                    dtime(23, 59, 59))
        return datetime.fromisoformat(raw)
    except ValueError:
        raise ToolError(f"as_of must be YYYY-MM-DD or ISO datetime, got {raw!r}")


def enrolled_machines(state, args):
    registry = load_registry()
    wanted = args.get("machine_id")
    if wanted:
        if wanted not in registry:
            raise ToolError(f"machine {wanted} is not enrolled — "
                            f"unenrolled machines are invisible to Pantry")
        return [wanted]
    return sorted(registry)


def latest_read(state, machine_id):
    reads = state["reads"].get(machine_id, [])
    return reads[-1] if reads else None


def staleness(snap, as_of):
    read_at = datetime.fromisoformat(snap["read_at"])
    age_h = round((as_of - read_at).total_seconds() / 3600.0, 1)
    return age_h, age_h > STALE_AFTER_HOURS


def t_inventory_read(state, args):
    as_of = parse_as_of(args)
    out = []
    stale_ids = []
    for mid in enrolled_machines(state, args):
        snap = latest_read(state, mid)
        reg = load_registry()[mid]
        if snap is None:
            out.append({"machine_id": mid, "type": reg["type"],
                        "location": reg["location"],
                        "platform_source": reg["platform_source"],
                        "read_at": None,
                        "note": "Enrolled, but no telemetry read ingested yet."})
            continue
        age_h, stale = staleness(snap, as_of)
        if stale:
            stale_ids.append(mid)
        rows = []
        for s in snap["selections"]:
            rows.append({**s,
                         "fill_pct": round(100.0 * s["inventory"]
                                           / s["capacity"], 1)
                         if s["capacity"] else None,
                         "provenance": snap["provenance"]})
        out.append({"machine_id": mid, "type": snap["machine_type"],
                    "location": snap["location"],
                    "platform_source": snap["platform_source"],
                    "read_at": snap["read_at"], "age_hours": age_h,
                    "stale": stale, "stale_after_hours": STALE_AFTER_HOURS,
                    "selections": rows})
    r = emit_receipt(state, "inventory_read",
                     {"machines": [m["machine_id"] for m in out],
                      "stale_machines": stale_ids,
                      "as_of": as_of.isoformat()})
    return {"machines": out, "count": len(out), "receipt_seq": r["seq"],
            "note": "Tier 0 read. Stock figures are the machine's own "
                    "telemetry as of read_at — a stale read is labeled, "
                    "never silently freshened."}


def _cashless_share(cash):
    total = cash["cash_cents"] + cash["cashless_cents"]
    if not total:
        return None
    return round(100.0 * cash["cashless_cents"] / total, 2)


def _read_at_or_before(reads, when):
    best = None
    for s in reads:
        if datetime.fromisoformat(s["read_at"]) <= when:
            best = s
    return best


def t_sales_summary(state, args):
    machines, detail = [], []
    tot = {"vends": 0, "gross_cents": 0, "cash_cents": 0,
           "cashless_cents": 0, "cashless_count": 0}
    since = args.get("since")
    until = args.get("until")
    for mid in enrolled_machines(state, args):
        reads = state["reads"].get(mid, [])
        if not reads:
            continue
        if since and until:
            since_dt = datetime.combine(
                datetime.fromisoformat(str(since)).date(), dtime(0, 0, 0))
            until_dt = datetime.combine(
                datetime.fromisoformat(str(until)).date(), dtime(23, 59, 59))
            end = _read_at_or_before(reads, until_dt)
            if end is None:
                continue
            base = _read_at_or_before(reads, since_dt)
            if base is None:
                # No read at/before the period start: bracket with the
                # earliest read inside the window, if there are two.
                window = [s for s in reads
                          if since_dt <= datetime.fromisoformat(s["read_at"])
                          <= until_dt]
                if len(window) >= 2:
                    base = window[0]
            if base is None or base is end:
                agg, mode = end, "cumulative_since_reset"
                dv = sum(s["vends"] for s in end["selections"])
                dg = sum(s["value_cents"] for s in end["selections"])
                cash = dict(end["cash"])
            else:
                mode = "period_delta"
                bmap = {s["selection"]: s for s in base["selections"]}
                dv = sum(s["vends"] - bmap.get(s["selection"], {})
                         .get("vends", 0) for s in end["selections"])
                dg = sum(s["value_cents"] - bmap.get(s["selection"], {})
                         .get("value_cents", 0) for s in end["selections"])
                cash = {k: end["cash"][k] - base["cash"][k]
                        for k in end["cash"]}
        else:
            end = reads[-1]
            agg, mode = end, "cumulative_since_reset"
            dv = sum(s["vends"] for s in end["selections"])
            dg = sum(s["value_cents"] for s in end["selections"])
            cash = dict(end["cash"])
        machines.append(mid)
        detail.append({"machine_id": mid, "mode": mode,
                       "read_at": end["read_at"], "vends": dv,
                       "gross_cents": dg, "cash": cash,
                       "cashless_share_pct": _cashless_share(cash)})
        tot["vends"] += dv
        tot["gross_cents"] += dg
        for k in ("cash_cents", "cashless_cents", "cashless_count"):
            tot[k] += cash[k]
    tot["cashless_share_pct"] = _cashless_share(tot)
    r = emit_receipt(state, "sales_summary",
                     {"machines": machines, "vends": tot["vends"],
                      "gross_cents": tot["gross_cents"],
                      "mode": "period" if (since and until) else "cumulative"})
    return {"machines": detail, "totals": tot, "receipt_seq": r["seq"],
            "note": "Tier 0 read. Amounts are processor/machine aggregates "
                    "from DEX counters — no card data exists in this system."}


def _velocity(reads, sel):
    """Sales per day for one selection of a machine's reads.
    With 2+ reads: delta between the two most recent. With 1 read:
    cumulative vends since the counter reset. Returns (per_day, method)."""
    latest = reads[-1]
    row = next(s for s in latest["selections"]
               if s["selection"] == sel)
    if len(reads) >= 2:
        prev = reads[-2]
        prow = next((s for s in prev["selections"]
                     if s["selection"] == sel), None)
        dt_days = (datetime.fromisoformat(latest["read_at"])
                   - datetime.fromisoformat(prev["read_at"])
                   ).total_seconds() / 86400.0
        if prow is not None and dt_days > 0:
            dv = row["vends"] - prow["vends"]
            if dv >= 0:
                return dv / dt_days, "delta_between_reads"
    span_days = (datetime.fromisoformat(latest["read_at"])
                 - datetime.fromisoformat(latest["reset_at"])
                 ).total_seconds() / 86400.0
    if span_days <= 0:
        return 0.0, "cumulative_since_reset"
    return row["vends"] / span_days, "cumulative_since_reset"


def t_stockout_forecast(state, args):
    horizon = float(args.get("horizon_days", FORECAST_HORIZON_DEFAULT))
    out = []
    for mid in enrolled_machines(state, args):
        reads = state["reads"].get(mid, [])
        if not reads:
            continue
        latest = reads[-1]
        rows = []
        for s in latest["selections"]:
            vel, method = _velocity(reads, s["selection"])
            if vel > 0:
                days = round(s["inventory"] / vel, 2)
            else:
                days = None
            rows.append({
                "selection": s["selection"], "name": s["name"],
                "inventory": s["inventory"], "capacity": s["capacity"],
                "velocity_per_day": round(vel, 3), "method": method,
                "days_to_empty": days,
                "stocked_out": s["inventory"] == 0,
                "at_risk": days is not None and days <= horizon,
                "note": ("No measurable sales velocity — no forecast "
                         "offered." if days is None else
                         "Heuristic: constant velocity at the measured "
                         "rate. A prediction, labeled as one.")})
        rows.sort(key=lambda x: (x["days_to_empty"] is None,
                                 x["days_to_empty"] or 0))
        out.append({"machine_id": mid, "location": latest["location"],
                    "read_at": latest["read_at"],
                    "horizon_days": horizon, "selections": rows})
    at_risk = sum(1 for m in out for s in m["selections"] if s["at_risk"])
    r = emit_receipt(state, "stockout_forecast",
                     {"machines": [m["machine_id"] for m in out],
                      "at_risk": at_risk, "horizon_days": horizon})
    return {"machines": out, "at_risk_count": at_risk,
            "receipt_seq": r["seq"],
            "note": "Tier 0 read. Forecasts are labeled heuristics from "
                    "the fleet's own telemetry, not promises — the name "
                    "claims no never-empty guarantee."}


def t_fault_read(state, args):
    out = []
    for mid in enrolled_machines(state, args):
        snap = latest_read(state, mid)
        if snap is None:
            continue
        events = []
        for e in snap["events"]:
            events.append({**e,
                           "label": EVENT_LABELS.get(e["code"], e["code"]),
                           "severity": EVENT_SEVERITY.get(e["code"], "info")})
        out.append({"machine_id": mid, "location": snap["location"],
                    "read_at": snap["read_at"], "events": events,
                    "provenance": snap["provenance"]})
    total = sum(len(m["events"]) for m in out)
    r = emit_receipt(state, "fault_read",
                     {"machines": [m["machine_id"] for m in out],
                      "events": total})
    return {"machines": out, "event_count": total,
            "receipt_seq": r["seq"],
            "note": "Tier 0 read. C0 reports faults; it drafts no "
                    "tickets and dispatches nobody (that is C1)."}


def t_receipt_list(state, args):
    receipts = state["receipts"]
    rtype = args.get("type")
    if rtype:
        receipts = [e for e in receipts if e["type"] == rtype]
    limit = int(args.get("limit", 50))
    return {"receipts": receipts[-limit:], "count": len(receipts),
            "chain_valid": verify_receipt_chain(state["receipts"]),
            "note": "Append-only, hash-chained. receipt_list is the "
                    "audit reader and is itself not receipted."}


TOOLS = {
    "dex_ingest": (t_dex_ingest, "Ingest a DEX/UCS-style telemetry export from a LOCAL file. Fail-closed: malformed content, a PAN-shaped value anywhere in the file (PAN_DETECTED — card data has no field here), an unenrolled machine, or a serial mismatch rejects the whole ingest with the reason named; rejections are receipted too. Returns the normalized read summary with provenance.",
                   {"path": {"type": "string"}}, ["path"]),
    "inventory_read": (t_inventory_read, "Tier 0: product-level stock per enrolled machine from its latest telemetry read, with read age and a stale flag (>48h). Omit machine_id for the whole enrolled fleet; unenrolled machines are invisible.",
                       {"machine_id": {"type": "string"}, "as_of": {"type": "string"}}, []),
    "sales_summary": (t_sales_summary, "Tier 0: sales aggregates from DEX counters — vends, gross, and the cash vs cashless split (processor aggregates only; no card data exists in this system). Default: cumulative since counter reset, latest read per machine. With since+until (YYYY-MM-DD): deltas between the reads bracketing the period.",
                      {"machine_id": {"type": "string"}, "since": {"type": "string"}, "until": {"type": "string"}}, []),
    "stockout_forecast": (t_stockout_forecast, "Tier 0: days-to-empty per selection from measured sales velocity (delta between the two latest reads, else cumulative since reset) — a labeled heuristic. Flags selections stocked out or emptying within horizon_days (default 7).",
                          {"machine_id": {"type": "string"}, "horizon_days": {"type": "number"}}, []),
    "fault_read": (t_fault_read, "Tier 0: fault/event records from each machine's latest DEX read, with labels and severity. Reports only — no tickets, no dispatch (C1).",
                   {"machine_id": {"type": "string"}}, []),
    "receipt_list": (t_receipt_list, "The append-only, hash-chained receipt ledger: every ingest (accepted, duplicate, rejected) and every read verdict. Optional type filter and limit; reports chain validity.",
                     {"type": {"type": "string"}, "limit": {"type": "integer"}}, []),
}


def tool_list():
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props,
                             "required": req}}
            for name, (_, desc, props, req) in TOOLS.items()]


def handle(req: dict):
    method, rid = req.get("method", ""), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get(
                "protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "eclipse-pantry", "version": "0.1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602, "message": f"unknown tool {name}"}}
        state = load_state()
        try:
            out = TOOLS[name][0](state, args)
            save_state(state)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, indent=1)}]}}
        except ToolError as e:
            extra = {}
            if isinstance(e, DexError):
                extra = {"code": e.code, "line": e.line}
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": str(e), **extra})}],
                "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32601, "message": f"method not found: {method}"}}
    return None  # notification


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:  # never crash the transport
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
