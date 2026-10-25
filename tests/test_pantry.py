"""Eclipse Pantry v0.1 (C0) tests.

Hand-computed fixture expectations:
  DRINK-01 (reset 2026-10-10 09:15 -> read 2026-10-24 09:15 = 14.0 days)
    vends 84+56+42+28+14 = 224 ; gross 12600+8400+5250+4900+2100 = 33250
    cash 14650 / cashless 18600 (55.94%) / 118 txns
    A1 velocity 84/14 = 6.0/day, inv 16 -> 2.67 days to empty
    A5 inv 0 -> stocked out
  SNACK-01 (reset 2026-10-17 09:40 -> read 2026-10-24 09:40 = 7.0 days)
    vends 49+35+21+63 = 168 ; gross 6125+5250+4200+6300 = 21875
    cash 8750 / cashless 13125 (60.0%) / 96 txns
    B1 velocity 49/7 = 7.0/day, inv 11 -> 1.57 days ; B4 63/7 = 9/day, inv 2 -> 0.22
  COMBO-01 (reset 2026-10-03 10:05 -> read 2026-10-24 10:05 = 21.0 days)
    vends 105+42+84+21 = 252 ; gross 15750+23100+10500+6825 = 56175
    cash 16875 / cashless 39300 (69.96%) / 171 txns
  Fleet totals: vends 644 ; gross 111300 ; cashless 71025 ; cash 40275
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
FIX = ROOT / "fixtures"


class PantryCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.state_path = Path(self.td.name) / "state.json"
        os.environ["ECLIPSE_PANTRY_STATE"] = str(self.state_path)
        os.environ["ECLIPSE_PANTRY_REGISTRY"] = str(FIX / "registry.json")
        sys.path.insert(0, str(ROOT))
        import server
        self.server = importlib.reload(server)

    def tearDown(self):
        self.td.cleanup()

    # drive tools directly against a loaded state, like the transport does
    def call(self, name, args):
        state = self.server.load_state()
        out = self.server.TOOLS[name][0](state, args)
        self.server.save_state(state)
        return out

    def ingest_all(self):
        for name in ("dex-drink-01.txt", "dex-snack-01.txt",
                     "dex-combo-01.txt"):
            out = self.call("dex_ingest", {"path": str(FIX / name)})
            assert out["status"] == "ingested", out
        return out


class TestIngestAndTotals(PantryCase):
    def test_hand_computed_totals_per_machine(self):
        self.ingest_all()
        out = self.call("sales_summary", {})
        by_id = {m["machine_id"]: m for m in out["machines"]}
        self.assertEqual(by_id["DRINK-01"]["vends"], 224)
        self.assertEqual(by_id["DRINK-01"]["gross_cents"], 33250)
        self.assertEqual(by_id["DRINK-01"]["cash"]["cashless_cents"], 18600)
        self.assertEqual(by_id["DRINK-01"]["cash"]["cash_cents"], 14650)
        self.assertEqual(by_id["DRINK-01"]["cashless_share_pct"], 55.94)
        self.assertEqual(by_id["SNACK-01"]["vends"], 168)
        self.assertEqual(by_id["SNACK-01"]["gross_cents"], 21875)
        self.assertEqual(by_id["SNACK-01"]["cashless_share_pct"], 60.0)
        self.assertEqual(by_id["COMBO-01"]["vends"], 252)
        self.assertEqual(by_id["COMBO-01"]["gross_cents"], 56175)
        self.assertEqual(by_id["COMBO-01"]["cashless_share_pct"], 69.96)

    def test_fleet_totals(self):
        self.ingest_all()
        tot = self.call("sales_summary", {})["totals"]
        self.assertEqual(tot["vends"], 644)
        self.assertEqual(tot["gross_cents"], 111300)
        self.assertEqual(tot["cashless_cents"], 71025)
        self.assertEqual(tot["cash_cents"], 40275)
        self.assertEqual(tot["cashless_count"], 385)

    def test_ingest_provenance(self):
        out = self.call("dex_ingest",
                        {"path": str(FIX / "dex-drink-01.txt")})
        prov = out["provenance"]
        self.assertEqual(prov["dex_serial"], "SN-DRK-1001")
        self.assertEqual(prov["audit_counter"], 482)
        self.assertEqual(prov["source_file"], "dex-drink-01.txt")
        inv = self.call("inventory_read", {"machine_id": "DRINK-01",
                                           "as_of": "2026-10-25"})
        sel = inv["machines"][0]["selections"][0]
        self.assertEqual(sel["provenance"]["dex_serial"], "SN-DRK-1001")
        self.assertEqual(sel["selection"], "A1")
        self.assertEqual(sel["inventory"], 16)
        self.assertEqual(sel["fill_pct"], 40.0)

    def test_duplicate_ingest_is_ignored(self):
        self.call("dex_ingest", {"path": str(FIX / "dex-drink-01.txt")})
        out = self.call("dex_ingest",
                        {"path": str(FIX / "dex-drink-01.txt")})
        self.assertEqual(out["status"], "duplicate_ignored")
        tot = self.call("sales_summary", {})["totals"]
        self.assertEqual(tot["vends"], 224)  # not double-counted

    def test_period_delta_summary(self):
        self.call("dex_ingest", {"path": str(FIX / "dex-drink-01.txt")})
        text = (FIX / "dex-drink-01.txt").read_text()
        text = text.replace("DXS*SN-DRK-1001*DEXAUDIT*VA*V1.6*000482",
                            "DXS*SN-DRK-1001*DEXAUDIT*VA*V1.6*000483")
        text = text.replace("DT1*20261024*0915", "DT1*20261025*0915")
        text = text.replace("VA2*A1*84*12600*0", "VA2*A1*90*13500*0")
        text = text.replace("CA3*14650*18600*118", "CA3*14950*19200*124")
        p2 = Path(self.td.name) / "dex-drink-02.txt"
        p2.write_text(text)
        out = self.call("dex_ingest", {"path": str(p2)})
        self.assertEqual(out["status"], "ingested")
        s = self.call("sales_summary", {"since": "2026-10-24",
                                        "until": "2026-10-26"})
        m = s["machines"][0]
        self.assertEqual(m["mode"], "period_delta")
        self.assertEqual(m["vends"], 6)          # 90 - 84 on A1 only
        self.assertEqual(m["gross_cents"], 900)  # 13500 - 12600
        self.assertEqual(m["cash"]["cashless_cents"], 600)
        self.assertEqual(m["cash"]["cash_cents"], 300)


class TestFailClosed(PantryCase):
    def test_malformed_rejected_with_line_ref(self):
        out = self.call("dex_ingest",
                        {"path": str(FIX / "dex-malformed.txt")})
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["code"], "DEX_PARSE_ERROR")
        self.assertEqual(out["line"], 7)  # VA2*A1*many*... is line 7
        self.assertIn("line 7", out["error"])
        # nothing stored
        state = self.server.load_state()
        self.assertEqual(state["reads"], {})
        # ...but the rejection IS receipted
        types = [r["type"] for r in state["receipts"]]
        self.assertIn("dex_ingest_rejected", types)

    def test_pan_rejected_and_never_stored(self):
        out = self.call("dex_ingest",
                        {"path": str(FIX / "dex-with-pan.txt")})
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["code"], "PAN_DETECTED")
        self.assertIn("line 9", out["error"])
        # the PAN value is echoed NOWHERE: not in the error, not on disk
        self.assertNotIn("4111", out["error"])
        self.assertNotIn("4111", self.state_path.read_text())
        state = self.server.load_state()
        self.assertEqual(state["reads"], {})

    def test_unenrolled_machine_invisible(self):
        ghost = Path(self.td.name) / "dex-ghost.txt"
        ghost.write_text(
            "DXS*SN-GHOST-99*DEXAUDIT*VA*V1.6*000001\n"
            "ID1*GHOST-99*drink\n"
            "DT1*20261024*0800\n"
            "RS1*20261020*0800\n"
            "PA1*A1*Cola*150\n"
            "VA2*A1*10*1500*0\n"
            "IN1*A1*5*40\n"
            "CA3*1500*0*0\n"
            "SE1*9\n"
            "DXE*1\n")
        out = self.call("dex_ingest", {"path": str(ghost)})
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["code"], "MACHINE_NOT_ENROLLED")
        with self.assertRaises(self.server.ToolError):
            self.call("inventory_read", {"machine_id": "GHOST-99"})
        inv = self.call("inventory_read", {})
        self.assertNotIn("GHOST-99",
                         [m["machine_id"] for m in inv["machines"]])

    def test_serial_mismatch_rejected(self):
        p = Path(self.td.name) / "dex-wrong-serial.txt"
        text = (FIX / "dex-drink-01.txt").read_text().replace(
            "SN-DRK-1001", "SN-DRK-9999")
        p.write_text(text)
        out = self.call("dex_ingest", {"path": str(p)})
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["code"], "SERIAL_MISMATCH")


class TestReads(PantryCase):
    def test_staleness_math(self):
        self.call("dex_ingest", {"path": str(FIX / "dex-drink-01.txt")})
        fresh = self.call("inventory_read",
                          {"machine_id": "DRINK-01",
                           "as_of": "2026-10-25T09:15"})
        m = fresh["machines"][0]
        self.assertEqual(m["age_hours"], 24.0)
        self.assertFalse(m["stale"])
        old = self.call("inventory_read",
                        {"machine_id": "DRINK-01",
                         "as_of": "2026-10-27T09:15"})
        m = old["machines"][0]
        self.assertEqual(m["age_hours"], 72.0)
        self.assertTrue(m["stale"])

    def test_forecast_sanity(self):
        self.ingest_all()
        out = self.call("stockout_forecast", {})
        by_id = {m["machine_id"]: {s["selection"]: s
                                   for s in m["selections"]}
                 for m in out["machines"]}
        a1 = by_id["DRINK-01"]["A1"]
        self.assertEqual(a1["velocity_per_day"], 6.0)
        self.assertEqual(a1["days_to_empty"], 2.67)
        self.assertEqual(a1["method"], "cumulative_since_reset")
        self.assertTrue(a1["at_risk"])
        a5 = by_id["DRINK-01"]["A5"]
        self.assertTrue(a5["stocked_out"])
        self.assertEqual(a5["days_to_empty"], 0.0)
        b4 = by_id["SNACK-01"]["B4"]
        self.assertEqual(b4["velocity_per_day"], 9.0)
        self.assertEqual(b4["days_to_empty"], 0.22)
        a3 = by_id["DRINK-01"]["A3"]  # 12.67 days — beyond the 7d horizon
        self.assertFalse(a3["at_risk"])
        # risk (<=7d): DRINK A1/A2/A4/A5, SNACK B1/B2/B4, COMBO C1/C2
        self.assertEqual(out["at_risk_count"], 9)

    def test_fault_read(self):
        self.ingest_all()
        out = self.call("fault_read", {"machine_id": "DRINK-01"})
        ev = out["machines"][0]["events"]
        codes = {e["code"] for e in ev}
        self.assertEqual(codes, {"SOLD_OUT", "TEMP_HIGH"})
        temp = next(e for e in ev if e["code"] == "TEMP_HIGH")
        self.assertEqual(temp["severity"], "high")
        self.assertEqual(temp["occurrences"], 1)


class TestReceiptsAndSurface(PantryCase):
    def test_receipt_chain_and_tamper_detection(self):
        self.ingest_all()
        self.call("inventory_read", {})
        out = self.call("receipt_list", {})
        self.assertTrue(out["chain_valid"])
        self.assertGreaterEqual(out["count"], 4)  # 3 ingests + 1 read
        state = self.server.load_state()
        state["receipts"][1]["detail"]["vends_since_reset"] = 999999
        self.server.save_state(state)
        out = self.call("receipt_list", {})
        self.assertFalse(out["chain_valid"])

    def test_tool_surface_is_tier0_only(self):
        names = set(self.server.TOOLS)
        self.assertEqual(names, {"dex_ingest", "inventory_read",
                                 "sales_summary", "stockout_forecast",
                                 "fault_read", "receipt_list"})
        for banned in ("price", "planogram", "route", "order", "ticket",
                       "dispatch", "restock"):
            self.assertFalse(any(banned in n for n in names),
                             f"write-capable tool leaked into C0: {banned}")


if __name__ == "__main__":
    unittest.main()
