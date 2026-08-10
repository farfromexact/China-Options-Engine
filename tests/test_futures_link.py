from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import futures_link
from eod_enrich import build_radar_summary


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime(2026, 8, 7, 17, 0, tzinfo=tz)


class FuturesLinkTests(unittest.TestCase):
    def test_main_writes_history_linkage_for_verified_history_products(self) -> None:
        source = json.loads(
            (Path("data") / "snapshots" / "2026-08-07.json").read_text(
                encoding="utf-8"
            )
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            latest_path = root / "latest.json"
            radar_path = root / "radar_latest.json"
            status_path = root / "last_run_status.json"
            snapshot_dir = root / "snapshots"
            snapshot_dir.mkdir()
            latest_path.write_text(json.dumps(source), encoding="utf-8")
            radar_path.write_text(
                json.dumps(build_radar_summary(source)), encoding="utf-8"
            )

            with (
                patch("futures_link.LATEST_PATH", latest_path),
                patch("futures_link.RADAR_LATEST_PATH", radar_path),
                patch("futures_link.STATUS_PATH", status_path),
                patch("futures_link.SNAPSHOT_DIR", snapshot_dir),
                patch("futures_link.datetime", FrozenDateTime),
                patch(
                    "futures_link.parse_future_rows",
                    return_value=(
                        {product: [] for product in futures_link.FUTURE_PRODUCTS},
                        source["futures"]["source_status"],
                    ),
                ),
                patch(
                    "futures_link.summarize_product",
                    side_effect=lambda product, _contracts: source["futures"][
                        "products"
                    ][product],
                ),
            ):
                futures_link.main()

            output = json.loads(latest_path.read_text(encoding="utf-8"))
            self.assertIn("history_futures_option_linkage", output)
            self.assertEqual(
                output["history_futures_option_linkage"]["IF"][
                    "direct_option_product"
                ],
                "IO",
            )
            self.assertEqual(
                json.loads(status_path.read_text(encoding="utf-8"))["futures_linkage"][
                    "status"
                ],
                "ok",
            )


if __name__ == "__main__":
    unittest.main()
