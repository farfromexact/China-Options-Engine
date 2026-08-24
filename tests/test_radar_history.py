from __future__ import annotations

import json
import math
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from eod_enrich import build_radar_summary, is_verified_snapshot, restore_latest_verified
from radar_history import (
    build_history_document,
    build_history_record,
    load_history_records,
    rebuild_from_snapshots,
    upsert_record,
    write_history,
)


def sample_radar(market_date: str, *, fresh: bool = True, atm_iv: float = 0.2) -> dict:
    radar = {
        "date": market_date,
        "generated_at": f"{market_date}T15:55:00+08:00",
        "data_fresh": fresh,
        "previous_date": None,
        "source_status": {
            "option_chain": "ok",
            "volume": "ok",
            "official_eod": {"status": "ok", "trade_date": market_date.replace("-", "")},
            "official_quote_match_coverage": 1.0,
            "freshness": "fresh",
        },
        "products": {
            "IO": {
                "expiries": [
                    {
                        "symbol": "IO2609",
                        "expiry": "2026-09-18",
                        "forward": 4650.0,
                        "rows": [{"large": "payload must not enter history"}],
                        "metrics": {
                            "atm_iv": atm_iv,
                            "rr25": -0.01,
                            "pcr_oi": 0.8,
                            "gamma_peaks": [
                                {"strike": 4600, "abs_gamma_1pct": 100.0},
                                {"strike": 4700, "abs_gamma_1pct": 90.0},
                                {"strike": 4500, "abs_gamma_1pct": 80.0},
                                {"strike": 4800, "abs_gamma_1pct": 70.0},
                            ],
                        },
                    }
                ]
            }
        },
        "futures": {
            "source_status": {
                "status": "ok",
                "records": 16,
                "trade_date": market_date.replace("-", ""),
            },
            "products": {
                "IF": {
                    "status": "ok",
                    "main_contract": {"symbol": "IF2609", "close": 4645.6, "open": 4600.0},
                    "next_contract": {"symbol": "IF2612", "close": 4565.6},
                    "next_minus_main_points": -80.0,
                    "total_open_interest": 200000,
                }
            },
        },
        "futures_option_linkage": {
            "IF": {
                "direct_option_product": "IO",
                "direct_option_available": True,
                "matched_option_symbol": "IO2609",
                "future_minus_option_forward_points": -4.4,
                "option_metrics": {"large": "duplicate payload must not enter history"},
            }
        },
        "errors": [],
    }
    for product, forward in (("HO", 3250.0), ("MO", 7550.0)):
        cloned = deepcopy(radar["products"]["IO"])
        cloned["expiries"][0]["symbol"] = f"{product}2609"
        cloned["expiries"][0]["forward"] = forward
        radar["products"][product] = cloned

    for product, close in (("IH", 3245.0), ("IC", 6845.0), ("IM", 7545.0)):
        cloned = deepcopy(radar["futures"]["products"]["IF"])
        cloned["main_contract"]["symbol"] = f"{product}2609"
        cloned["main_contract"]["close"] = close
        cloned["next_contract"]["symbol"] = f"{product}2612"
        cloned["next_contract"]["close"] = close - 80.0
        radar["futures"]["products"][product] = cloned
    return radar


class RadarHistoryTests(unittest.TestCase):
    def test_build_record_is_compact_and_keeps_core_metrics(self) -> None:
        record = build_history_record(sample_radar("2026-08-07"))
        expiry = record["options"]["IO"]["expiries"][0]
        self.assertEqual(expiry["metrics"]["atm_iv"], 0.2)
        self.assertEqual(len(expiry["metrics"]["gamma_peaks"]), 3)
        self.assertNotIn("rows", expiry)
        self.assertNotIn("option_metrics", record["futures_option_linkage"]["IF"])
        self.assertEqual(record["futures"]["IF"]["main_contract"]["open"], 4600.0)
        self.assertEqual(expiry["tenor_rank"], 1)
        self.assertEqual(list(record["options"]), ["HO", "IO", "MO"])
        self.assertEqual(list(record["futures"]), ["IH", "IF", "IC", "IM"])

    def test_stale_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "data_fresh=true"):
            build_history_record(sample_radar("2026-08-07", fresh=False))

    def test_unverified_source_status_is_rejected(self) -> None:
        missing_freshness = sample_radar("2026-08-07")
        del missing_freshness["source_status"]["freshness"]
        with self.assertRaisesRegex(ValueError, "non-fresh source status"):
            build_history_record(missing_freshness)

        bad_official = sample_radar("2026-08-07")
        bad_official["source_status"]["official_eod"] = {"status": "missing"}
        with self.assertRaisesRegex(ValueError, "invalid official EOD status"):
            build_history_record(bad_official)

        missing_futures = sample_radar("2026-08-07")
        del missing_futures["futures"]["source_status"]
        with self.assertRaisesRegex(ValueError, "successful futures data"):
            build_history_record(missing_futures)

        partial_options = sample_radar("2026-08-07")
        partial_options["source_status"]["official_quote_match_coverage"] = 0.949
        with self.assertRaisesRegex(ValueError, "coverage below the 95% minimum"):
            build_history_record(partial_options)

        acceptable_options = sample_radar("2026-08-07")
        acceptable_options["source_status"]["official_quote_match_coverage"] = 0.95
        self.assertEqual(
            build_history_record(acceptable_options)["data_quality"][
                "official_quote_match_coverage"
            ],
            0.95,
        )

        empty_product = sample_radar("2026-08-07")
        empty_product["products"]["HO"]["expiries"] = []
        with self.assertRaisesRegex(ValueError, "no active HO expiries"):
            build_history_record(empty_product)

        incomplete_future = sample_radar("2026-08-07")
        incomplete_future["futures"]["products"]["IH"]["status"] = "missing"
        with self.assertRaisesRegex(ValueError, "incomplete IH futures data"):
            build_history_record(incomplete_future)

    def test_settlement_options_require_matching_settlement_linkage(self) -> None:
        source = sample_radar("2026-08-07")
        source["history_products"] = deepcopy(source["products"])
        source["history_option_price_basis"] = "cffex_official_settlement_fallback_close"
        with self.assertRaisesRegex(ValueError, "settlement-based futures linkage"):
            build_history_record(source)

        source["history_futures_option_linkage"] = deepcopy(
            source["futures_option_linkage"]
        )
        record = build_history_record(source)
        self.assertEqual(
            record["futures_option_linkage"]["IF"]["matched_option_symbol"],
            "IO2609",
        )

    def test_non_finite_numbers_are_normalized_to_null(self) -> None:
        source = sample_radar("2026-08-07", atm_iv=math.nan)
        record = build_history_record(source)
        self.assertIsNone(record["options"]["IO"]["expiries"][0]["metrics"]["atm_iv"])

    def test_upsert_replaces_same_date_and_sorts(self) -> None:
        newer = build_history_record(sample_radar("2026-08-08", atm_iv=0.22))
        older = build_history_record(sample_radar("2026-08-07", atm_iv=0.20))
        replacement = build_history_record(sample_radar("2026-08-08", atm_iv=0.23))
        records = upsert_record([newer, older], replacement)
        self.assertEqual([item["date"] for item in records], ["2026-08-07", "2026-08-08"])
        self.assertEqual(records[-1]["options"]["IO"]["expiries"][0]["metrics"]["atm_iv"], 0.23)

    def test_rebuild_skips_fresh_but_unverified_legacy_snapshots(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures" / "snapshots"
        records = rebuild_from_snapshots(fixture_dir)
        self.assertEqual(records, [])

    def test_rebuild_stops_after_retention_window(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures" / "bounded_snapshots"
        with patch("radar_history._validate_verified_sources"):
            records = rebuild_from_snapshots(fixture_dir, retention_sessions=2)
        self.assertEqual([item["date"] for item in records], ["2026-08-09", "2026-08-10"])

    def test_rebuild_rejects_filename_date_mismatch(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures" / "mismatched_snapshots"
        with self.assertRaisesRegex(ValueError, "filename/date mismatch"):
            rebuild_from_snapshots(fixture_dir)

    def test_committed_history_is_strict_and_deterministic(self) -> None:
        path = Path("data") / "radar_history.json"
        records = load_history_records(path)
        fixture_document = build_history_document(records)
        first = json.dumps(fixture_document, ensure_ascii=False, indent=2)
        second = json.dumps(build_history_document(records), ensure_ascii=False, indent=2)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(records), 1)

    def test_atomic_writer_is_a_true_noop_for_identical_bytes(self) -> None:
        document = build_history_document([build_history_record(sample_radar("2026-08-07"))])
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        path = Path("unused") / "radar_history.json"
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=payload),
            patch.object(Path, "write_text") as write_text,
            patch.object(Path, "replace") as replace,
        ):
            self.assertFalse(write_history(path, document))
            write_text.assert_not_called()
            replace.assert_not_called()

        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as write_text,
            patch.object(Path, "replace") as replace,
        ):
            self.assertTrue(write_history(path, document))
            write_text.assert_called_once_with(payload, encoding="utf-8")
            replace.assert_called_once_with(path)

        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
            patch.object(Path, "replace") as replace,
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                write_history(path, document)
            replace.assert_not_called()

    def test_restore_validator_rejects_pre_eod_and_preserves_linkage(self) -> None:
        verified = sample_radar("2026-08-07")
        self.assertTrue(is_verified_snapshot(verified, "2026-08-07"))

        acceptable_coverage = sample_radar("2026-08-07")
        acceptable_coverage["source_status"]["official_quote_match_coverage"] = 0.95
        self.assertTrue(is_verified_snapshot(acceptable_coverage, "2026-08-07"))

        insufficient_coverage = sample_radar("2026-08-07")
        insufficient_coverage["source_status"]["official_quote_match_coverage"] = 0.949
        self.assertFalse(is_verified_snapshot(insufficient_coverage, "2026-08-07"))

        pre_eod = sample_radar("2026-08-08")
        del pre_eod["source_status"]["official_eod"]
        self.assertFalse(is_verified_snapshot(pre_eod, "2026-08-08"))

        summary = build_radar_summary(verified)
        self.assertEqual(summary["futures"]["source_status"]["status"], "ok")
        self.assertEqual(summary["futures_option_linkage"]["IF"]["matched_option_symbol"], "IO2609")

    def test_restore_skips_corrupt_and_pre_eod_snapshots(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures" / "restore_snapshots"
        with (
            patch("eod_enrich.SNAPSHOT_DIR", fixture_dir),
            patch("eod_enrich.LATEST_PATH") as latest_path,
            patch("eod_enrich.RADAR_LATEST_PATH") as radar_path,
            patch(
                "eod_enrich.is_verified_snapshot",
                side_effect=lambda _snapshot, snapshot_date: snapshot_date == "2026-08-07",
            ),
        ):
            restored = restore_latest_verified()

        self.assertEqual(restored["date"], "2026-08-07")
        latest_path.write_text.assert_called_once()
        radar_path.write_text.assert_called_once()
        compact = json.loads(radar_path.write_text.call_args.args[0])
        self.assertEqual(compact["futures_option_linkage"]["IF"]["matched_option_symbol"], "IO2609")

    def test_history_retains_latest_sessions(self) -> None:
        start = date(2026, 1, 1)
        records = [
            build_history_record(sample_radar((start + timedelta(days=offset)).isoformat()))
            for offset in range(61)
        ]
        document = build_history_document(records)
        self.assertEqual(document["retention_sessions"], 60)
        self.assertEqual(document["record_count"], 60)
        self.assertEqual(document["first_date"], "2026-01-02")
        self.assertEqual(document["latest_date"], "2026-03-02")
        self.assertIsNone(document["records"][0]["previous_date"])
        self.assertEqual(document["records"][1]["previous_date"], "2026-01-02")

    def test_strict_schema_rejects_missing_products_and_nonfinite_values(self) -> None:
        record = build_history_record(sample_radar("2026-08-07"))
        record["options"] = {}
        with self.assertRaisesRegex(ValueError, "unexpected schema"):
            build_history_document([record])

        record = build_history_record(sample_radar("2026-08-07"))
        record["futures"]["IF"]["main_contract"]["close"] = math.inf
        with self.assertRaisesRegex(ValueError, "non-finite"):
            build_history_document([record])

    def test_current_snapshot_maps_expected_coverage(self) -> None:
        source = json.loads((Path("data") / "snapshots" / "2026-08-07.json").read_text(encoding="utf-8"))
        record = build_history_record(source)
        self.assertEqual(sum(len(item["expiries"]) for item in record["options"].values()), 12)
        self.assertEqual(sum(bool(item["main_contract"]["symbol"]) for item in record["futures"].values()), 4)
        self.assertEqual(
            sum(bool(record["futures_option_linkage"][product]["matched_option_symbol"]) for product in ("IH", "IF", "IM")),
            3,
        )
        self.assertIsNone(record["futures_option_linkage"]["IC"]["matched_option_symbol"])


if __name__ == "__main__":
    unittest.main()
