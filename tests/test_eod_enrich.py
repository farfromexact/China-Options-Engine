from __future__ import annotations

import csv
import io
import json
import shutil
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import eod_enrich


FIELDNAMES = (
    "合约代码",
    "成交量",
    "持仓量",
    "持仓变化",
    "今收盘",
    "今结算",
    "前结算",
    "Delta",
)


def valid_daily_csv() -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FIELDNAMES)
    writer.writeheader()
    writer.writerow(
        {
            "合约代码": "IO2608-C-4700",
            "成交量": "100",
            "持仓量": "500",
            "持仓变化": "10",
            "今收盘": "120.5",
            "今结算": "121.0",
            "前结算": "119.0",
            "Delta": "0.5",
        }
    )
    return output.getvalue().encode("utf-8-sig")


def monthly_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return output.getvalue()


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime(2026, 8, 8, 18, 35, tzinfo=tz)


class EodEnrichTests(unittest.TestCase):
    def test_parse_failure_falls_back_to_monthly_zip(self) -> None:
        market_date = date(2026, 8, 6)
        archive = monthly_zip({"nested/20260806_1.csv": valid_daily_csv()})
        with (
            patch(
                "eod_enrich.download_single_daily_csv",
                return_value=("https://cffex.example/daily.csv", b"<html>blocked</html>"),
            ),
            patch(
                "eod_enrich.download_monthly_zip",
                return_value=("https://cffex.example/202608.zip", archive),
            ),
        ):
            records, status = eod_enrich.fetch_cffex_eod(market_date)

        self.assertEqual(status["status"], "ok")
        self.assertIn("#nested/20260806_1.csv", status["source"])
        self.assertIn("IO2608C4700", records)

    def test_absent_verified_archive_member_is_marked_non_trading(self) -> None:
        market_date = date(2026, 8, 6)
        archive = monthly_zip({"20260805_1.csv": valid_daily_csv()})
        with (
            patch(
                "eod_enrich.download_single_daily_csv",
                return_value=("https://cffex.example/daily.csv", b"<html>holiday</html>"),
            ),
            patch(
                "eod_enrich.download_monthly_zip",
                return_value=("https://cffex.example/202608.zip", archive),
            ),
        ):
            records, status = eod_enrich.fetch_cffex_eod(market_date)

        self.assertEqual(records, {})
        self.assertEqual(status["status"], "not_trading")
        self.assertEqual(status["trade_date"], "20260806")

    def test_trade_date_selection_uses_last_completed_weekday_before_ready_time(self) -> None:
        before_ready = datetime(2026, 9, 24, 6, 20, tzinfo=eod_enrich.TZ_CN)
        after_ready = datetime(2026, 9, 24, 18, 30, tzinfo=eod_enrich.TZ_CN)
        weekend = datetime(2026, 9, 27, 18, 30, tzinfo=eod_enrich.TZ_CN)

        self.assertEqual(eod_enrich.select_initial_trade_date(before_ready), date(2026, 9, 23))
        self.assertEqual(eod_enrich.select_initial_trade_date(after_ready), date(2026, 9, 24))
        self.assertEqual(eod_enrich.select_initial_trade_date(weekend), date(2026, 9, 25))

    def test_resolve_skips_verified_non_trading_dates(self) -> None:
        requested = date(2026, 9, 28)
        resolved = date(2026, 9, 25)
        with patch(
            "eod_enrich.fetch_cffex_eod",
            side_effect=[
                ({}, {"status": "not_trading", "trade_date": "20260928"}),
                ({"IO2608C4700": {}}, {"status": "ok", "trade_date": "20260925"}),
            ],
        ):
            trade_date, records, status = eod_enrich.resolve_latest_completed_eod(
                datetime(2026, 9, 28, 18, 35, tzinfo=eod_enrich.TZ_CN)
            )

        self.assertEqual(trade_date, resolved)
        self.assertEqual(records, {"IO2608C4700": {}})
        self.assertEqual(status["requested_trade_date"], requested.isoformat())
        self.assertEqual(status["skipped_non_trading_dates"], [requested.isoformat()])

    def test_main_persists_failure_status_and_restores_verified_snapshot(self) -> None:
        verified = json.loads(
            (Path("data") / "snapshots" / "2026-08-07.json").read_text(encoding="utf-8")
        )
        root = Path.cwd() / f".test-eod-enrich-{uuid4().hex}"
        root.mkdir()
        self.addCleanup(shutil.rmtree, root, True)
        snapshot_dir = root / "snapshots"
        snapshot_dir.mkdir()
        (snapshot_dir / "2026-08-07.json").write_text(
            json.dumps(verified), encoding="utf-8"
        )
        latest_path = root / "latest.json"
        latest_path.write_text('{"date": "2026-08-08"}', encoding="utf-8")
        radar_path = root / "radar_latest.json"
        status_path = root / "last_run_status.json"

        with (
            patch("eod_enrich.DATA_DIR", root),
            patch("eod_enrich.SNAPSHOT_DIR", snapshot_dir),
            patch("eod_enrich.LATEST_PATH", latest_path),
            patch("eod_enrich.RADAR_LATEST_PATH", radar_path),
            patch("eod_enrich.STATUS_PATH", status_path),
            patch("eod_enrich.datetime", FrozenDateTime),
            patch(
                "eod_enrich.resolve_latest_completed_eod",
                return_value=(
                    None,
                    {},
                    {
                        "status": "missing",
                        "requested_trade_date": "2026-08-08",
                        "error": "CFFEX daily CSV has no header",
                    },
                ),
            ),
        ):
            with self.assertRaisesRegex(
                eod_enrich.CffexEodUnavailable, "CFFEX daily CSV has no header"
            ):
                eod_enrich.main()

        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertFalse(status["data_fresh"])
        self.assertEqual(status["failure_kind"], "cffex_eod_unavailable")
        self.assertEqual(
            json.loads(latest_path.read_text(encoding="utf-8"))["date"], "2026-08-07"
        )

    def test_workflow_persists_then_fails_a_non_fresh_eod_run(self) -> None:
        workflow = (Path(".github") / "workflows" / "daily.yml").read_text(
            encoding="utf-8"
        )
        eod_step = workflow.index("id: eod")
        persist_step = workflow.index("Persist CFFEX EOD failure status")
        fail_step = workflow.index("Fail when fresh CFFEX EOD was not published")
        linkage_step = workflow.index("Link IH IF IC IM futures")

        self.assertIn("continue-on-error: true", workflow[eod_step:persist_step])
        self.assertLess(eod_step, persist_step)
        self.assertLess(persist_step, fail_step)
        self.assertLess(fail_step, linkage_step)

    def test_scheduler_bridge_skips_pre_eod_dispatches(self) -> None:
        bridge = (Path(".github") / "workflows" / "chatgpt-trigger.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("TZ=Asia/Shanghai", bridge)
        self.assertIn("-lt 1830", bridge)
        self.assertIn("steps.eod_window.outputs.dispatch == 'true'", bridge)


if __name__ == "__main__":
    unittest.main()
