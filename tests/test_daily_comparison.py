from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from daily_comparison import add_changes, previous_snapshot, prior_session
from cash_market import _load_previous_cash_market


def snapshot(day, iv):
    expiry = {"symbol": "IO2611", "metrics": {"atm_iv": iv, "call_oi": 8, "atm_iv_change_1d": 99}}
    return {
        "date": day,
        "data_fresh": True,
        "assumptions": {"iv_price": "book"},
        "source_status": {
            "freshness": "fresh", "option_chain": "ok", "volume": "ok",
            "official_quote_match_coverage": 1.0,
            "official_eod": {"status": "ok", "trade_date": day},
        },
        "products": {"IO": {"expiries": [expiry]}},
    }


class DailyComparisonTests(unittest.TestCase):
    def test_gap_and_repricing_clear_provisional_changes(self):
        curr = snapshot("2026-10-22", .21)
        old = snapshot("2026-10-20", .18)
        metrics = curr["products"]["IO"]["expiries"][0]["metrics"]
        add_changes(curr, old)
        self.assertIsNone(curr["previous_date"])
        self.assertNotIn("atm_iv_change_1d", metrics)
        old = snapshot("2026-10-21", .18)
        add_changes(curr, old)
        self.assertAlmostEqual(metrics["atm_iv_change_1d"], .03)
        old["assumptions"]["iv_price"] = "settlement"
        add_changes(curr, old)
        self.assertNotIn("atm_iv_change_1d", metrics)
        self.assertEqual(metrics["call_oi_change_1d"], 0)

    def test_contract_roll_and_partial_volume_do_not_create_daily_change(self):
        current = snapshot("2026-10-22", .21)
        previous = snapshot("2026-10-21", .18)
        metrics = current["products"]["IO"]["expiries"][0]["metrics"]
        old = previous["products"]["IO"]["expiries"][0]
        metrics.update(pcr_volume=1.2, call_oi_change=6)
        old["metrics"]["pcr_volume_partial"] = .8
        add_changes(current, previous)
        self.assertNotIn("pcr_volume_change_1d", metrics)
        old["symbol"] = "IO2612"
        add_changes(current, previous)
        self.assertFalse(any(key.endswith("_change_1d") for key in metrics))
        self.assertEqual(metrics["call_oi_change"], 6)

    def test_calendar_and_snapshot_identity(self):
        self.assertEqual(prior_session(date(2026, 10, 8)), date(2026, 9, 30))
        self.assertEqual(prior_session(date(2026, 11, 2)), date(2026, 10, 30))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2026, 10, 22)
            payload = snapshot("2026-10-20", .18)
            payload["futures"] = {"cash_market": {"trade_date": "2026-10-20"}}
            (root / "2026-10-20.json").write_text(json.dumps(payload))
            self.assertIsNone(_load_previous_cash_market(root, day))
            path = root / "2026-10-21.json"
            path.write_text(json.dumps(payload))
            self.assertIsNone(previous_snapshot(root, day))
            payload["date"] = "2026-10-21"
            path.write_text(json.dumps(payload))
            self.assertIsNone(previous_snapshot(root, day))
            payload["source_status"]["official_eod"]["trade_date"] = "2026-10-21"
            path.write_text(json.dumps(payload))
            self.assertIsNotNone(previous_snapshot(root, day))
            self.assertIsNone(_load_previous_cash_market(root, day))
            payload["data_fresh"] = False
            path.write_text(json.dumps(payload))
            self.assertIsNone(previous_snapshot(root, day))
