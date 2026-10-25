#!/usr/bin/env python3
"""Smoke: drive server.py over real stdio with a temp state file.
Ingests the three synthetic DEX fixtures (plus the two rejection
fixtures), runs the Tier-0 reads, prints a compact transcript.
Uses NO real data and makes NO network calls."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent


def call(proc, rid, name, args):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "method": "tools/call",
                                 "params": {"name": name,
                                            "arguments": args}}) + "\n")
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    return json.loads(resp["result"]["content"][0]["text"])


def main():
    with tempfile.TemporaryDirectory() as td:
        env = {"ECLIPSE_PANTRY_STATE": str(Path(td) / "state.json"),
               "ECLIPSE_PANTRY_REGISTRY": str(ROOT / "fixtures"
                                              / "registry.json"),
               "PATH": "/usr/bin:/bin:/usr/local/bin"}
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, env=env)
        try:
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                 "params": {"protocolVersion": "2025-03-26"}}) + "\n")
            proc.stdin.flush()
            init = json.loads(proc.stdout.readline())
            print("server:", init["result"]["serverInfo"])
            rid = 1
            for name in ("dex-drink-01.txt", "dex-snack-01.txt",
                         "dex-combo-01.txt"):
                out = call(proc, rid, "dex_ingest",
                           {"path": str(ROOT / "fixtures" / name)})
                rid += 1
                print(f"ingest {name}: {out['status']} "
                      f"({out['machine_id']}, {out['totals']['vends']} vends)")
            for name in ("dex-malformed.txt", "dex-with-pan.txt"):
                out = call(proc, rid, "dex_ingest",
                           {"path": str(ROOT / "fixtures" / name)})
                rid += 1
                print(f"ingest {name}: {out['status']} [{out['code']}]")
            s = call(proc, rid, "sales_summary", {})
            rid += 1
            print("fleet totals:", s["totals"])
            f = call(proc, rid, "stockout_forecast", {"horizon_days": 7})
            rid += 1
            print("forecast at-risk selections:", f["at_risk_count"])
            fl = call(proc, rid, "fault_read", {})
            rid += 1
            print("fault events:", fl["event_count"])
            rc = call(proc, rid, "receipt_list", {})
            rid += 1
            print("receipts:", rc["count"], "chain_valid:",
                  rc["chain_valid"])
        finally:
            proc.stdin.close()
            proc.terminate()


if __name__ == "__main__":
    main()
