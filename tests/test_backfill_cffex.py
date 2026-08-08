from __future__ import annotations

import csv
import io
import os
import shutil
import unittest
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backfill_cffex import (
    SETTLEMENT_PRICE_BASIS,
    KnownNonTradingDay,
    MonthlyCsvProvider,
    _publish_staged,
    build_verified_snapshot,
    collect_prior_sessions,
    enrich_anchor_history,
    parse_option_contract,
)
from engine import RISK_FREE_RATE, TZ_CN, black76_price, expiry_from_symbol, year_fraction
from eod_enrich import (
    HISTORY_PRICE_FIELD,
    build_official_settlement_products,
    build_settlement_history_products,
    create_session,
    is_verified_snapshot,
    parse_cffex_eod_content,
)
from radar_history import build_history_record


FIELDNAMES = (
    "合约代码",
    "今开盘",
    "最高价",
    "最低价",
    "成交量",
    "成交金额",
    "持仓量",
    "持仓变化",
    "今收盘",
    "今结算",
    "前结算",
    "Delta",
)


def sample_daily_csv(market_date: date) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FIELDNAMES)
    writer.writeheader()

    option_forwards = {"HO": 3300.0, "IO": 4700.0, "MO": 7500.0}
    expiry_symbol = "2608"
    expiry = expiry_from_symbol(f"IO{expiry_symbol}")
    now = datetime.combine(market_date, time(15, 0), tzinfo=TZ_CN)
    t = year_fraction(expiry, now)
    for product, forward in option_forwards.items():
        for strike in (forward - 100.0, forward, forward + 100.0):
            for cp in ("C", "P"):
                settle = black76_price(cp, forward, strike, t, RISK_FREE_RATE, 0.20)
                writer.writerow(
                    {
                        "合约代码": f"{product}{expiry_symbol}-{cp}-{int(strike)}",
                        "今开盘": settle,
                        "最高价": settle,
                        "最低价": settle,
                        "成交量": 100,
                        "成交金额": 1000,
                        "持仓量": 500,
                        "持仓变化": 10,
                        "今收盘": settle,
                        "今结算": settle,
                        "前结算": settle,
                        "Delta": 0.5 if cp == "C" else -0.5,
                    }
                )

    future_closes = {"IH": 3300.0, "IF": 4700.0, "IC": 6800.0, "IM": 7500.0}
    for product, close in future_closes.items():
        for month, volume in (("2608", 1000), ("2609", 500)):
            writer.writerow(
                {
                    "合约代码": f"{product}{month}",
                    "今开盘": close - 10,
                    "最高价": close + 20,
                    "最低价": close - 20,
                    "成交量": volume,
                    "成交金额": 100000,
                    "持仓量": volume * 2,
                    "持仓变化": 50,
                    "今收盘": close,
                    "今结算": close,
                    "前结算": close - 5,
                    "Delta": "",
                }
            )
    return output.getvalue().encode("utf-8-sig")


def monthly_zip(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return output.getvalue()


class BackfillCffexTests(unittest.TestCase):
    def test_publish_installs_a_copy_from_private_staging(self) -> None:
        root = Path.cwd() / f".test-backfill-{uuid4().hex}"
        root.mkdir()
        self.addCleanup(shutil.rmtree, root, True)
        staging = root / "private-staging"
        snapshots = root / "snapshots"
        staging.mkdir()
        snapshots.mkdir()
        staged_path = staging / "2026-08-06.json"
        target = snapshots / staged_path.name
        staged_path.write_text('{"version": 2}\n', encoding="utf-8")
        target.write_text('{"version": 1}\n', encoding="utf-8")

        _publish_staged(
            [(staged_path, staged_path.name)],
            snapshots,
            overwrite=False,
            replace_existing={staged_path.name},
        )

        self.assertTrue(staged_path.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), '{"version": 2}\n')
        self.assertEqual(list(snapshots.glob(".*.tmp")), [])

    def test_post_publish_failure_rolls_back_all_snapshots(self) -> None:
        root = Path.cwd() / f".test-backfill-{uuid4().hex}"
        root.mkdir()
        self.addCleanup(shutil.rmtree, root, True)
        staging = root / "private-staging"
        snapshots = root / "snapshots"
        staging.mkdir()
        snapshots.mkdir()
        existing_stage = staging / "2026-08-06.json"
        new_stage = staging / "2026-08-07.json"
        existing_stage.write_text("replacement\n", encoding="utf-8")
        new_stage.write_text("new\n", encoding="utf-8")
        existing_target = snapshots / existing_stage.name
        existing_target.write_text("original\n", encoding="utf-8")

        def fail_validation() -> None:
            raise RuntimeError("history validation failed")

        with self.assertRaisesRegex(RuntimeError, "history validation failed"):
            _publish_staged(
                [(existing_stage, existing_stage.name), (new_stage, new_stage.name)],
                snapshots,
                overwrite=True,
                post_publish=fail_validation,
            )

        self.assertEqual(existing_target.read_text(encoding="utf-8"), "original\n")
        self.assertFalse((snapshots / new_stage.name).exists())

    def test_parse_option_contract(self) -> None:
        self.assertEqual(parse_option_contract("IO2608C4700"), ("IO", "2608", "C", 4700.0))
        self.assertIsNone(parse_option_contract("IF2608"))

    def test_historical_snapshot_is_verified_and_explicitly_settlement_based(self) -> None:
        market_date = date(2026, 8, 6)
        snapshot = build_verified_snapshot(
            market_date,
            "https://www.cffex.com.cn/example/20260806_1.csv",
            sample_daily_csv(market_date),
        )

        self.assertTrue(is_verified_snapshot(snapshot, market_date.isoformat()))
        expiry = snapshot["products"]["IO"]["expiries"][0]
        self.assertEqual(expiry["metrics"]["pricing_basis"], SETTLEMENT_PRICE_BASIS)
        self.assertAlmostEqual(expiry["forward"], 4700.0, places=4)
        self.assertAlmostEqual(expiry["metrics"]["atm_iv"], 0.20, places=4)
        quote = expiry["rows"][0]["call"]
        self.assertNotIn("bid", quote)
        self.assertEqual(quote["analytics_price_field"], HISTORY_PRICE_FIELD)

        history = build_history_record(snapshot)
        self.assertIn("historical backfill", history["data_quality"]["freshness_method"])
        self.assertEqual(history["data_quality"]["futures_status"], "ok")
        self.assertEqual(history["data_quality"]["record_origin"], "historical_eod_backfill")
        self.assertEqual(history["data_quality"]["option_price_basis"], SETTLEMENT_PRICE_BASIS)

    def test_anchor_history_is_repriced_without_replacing_live_products(self) -> None:
        market_date = date(2026, 8, 7)
        settlement = build_verified_snapshot(
            market_date,
            "https://www.cffex.com.cn/example/20260807_1.csv",
            sample_daily_csv(market_date),
        )
        anchor = dict(settlement)
        anchor["products"] = {**settlement["products"], "live_marker": {"expiries": []}}
        anchor.pop("history_products", None)
        enriched = enrich_anchor_history(anchor, settlement, date(2026, 8, 6))

        self.assertIn("live_marker", enriched["products"])
        self.assertNotIn("live_marker", enriched["history_products"])
        self.assertEqual(enriched["previous_date"], "2026-08-06")
        history = build_history_record(enriched)
        self.assertEqual(history["data_quality"]["record_origin"], "scheduled_eod")
        self.assertEqual(history["data_quality"]["option_price_basis"], SETTLEMENT_PRICE_BASIS)

    def test_daily_history_products_reprice_official_settlements(self) -> None:
        market_date = date(2026, 8, 6)
        snapshot = build_verified_snapshot(
            market_date,
            "https://www.cffex.com.cn/example/20260806_1.csv",
            sample_daily_csv(market_date),
        )
        history_products = build_settlement_history_products(snapshot["products"], market_date)
        self.assertAlmostEqual(
            history_products["IO"]["expiries"][0]["metrics"]["atm_iv"], 0.20, places=4
        )

    def test_official_history_builder_rejects_an_unpaired_active_strike(self) -> None:
        market_date = date(2026, 8, 6)
        official, _ = parse_cffex_eod_content(
            market_date,
            "https://www.cffex.com.cn/example/20260806_1.csv",
            sample_daily_csv(market_date),
        )
        products = build_official_settlement_products(official, market_date)
        self.assertEqual(list(products), ["HO", "IO", "MO"])

        unpaired = dict(official)
        symbol = next(key for key in unpaired if key.startswith("HO2608C"))
        del unpaired[symbol]
        with self.assertRaisesRegex(RuntimeError, "not call/put paired"):
            build_official_settlement_products(unpaired, market_date)

    def test_monthly_provider_fetches_archive_once_and_knows_missing_days(self) -> None:
        content = sample_daily_csv(date(2026, 8, 6))
        calls: list[str] = []

        def fetch_month(yyyymm: str) -> tuple[str, bytes]:
            calls.append(yyyymm)
            return "https://cffex.example/202608.zip", monthly_zip({"nested/20260806_1.csv": content})

        def unexpected_daily(_: date) -> tuple[str, bytes]:
            raise AssertionError("daily fallback should not run when the monthly ZIP is available")

        provider = MonthlyCsvProvider(fetch_month, unexpected_daily)
        first_source, first_content = provider.get(date(2026, 8, 6))
        second_source, second_content = provider.get(date(2026, 8, 6))

        self.assertEqual(calls, ["202608"])
        self.assertEqual(first_source, second_source)
        self.assertEqual(first_content, second_content)
        with self.assertRaises(KnownNonTradingDay):
            provider.get(date(2026, 8, 5))
        self.assertEqual(calls, ["202608"])

    def test_collect_prior_sessions_skips_weekend_and_returns_chronological_dates(self) -> None:
        dates = [date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6)]
        archive = monthly_zip(
            {f"{item.strftime('%Y%m%d')}_1.csv": sample_daily_csv(item) for item in dates}
        )
        provider = MonthlyCsvProvider(
            lambda _: ("https://cffex.example/202608.zip", archive),
            lambda _: (_ for _ in ()).throw(AssertionError("unexpected daily fallback")),
        )
        sessions = collect_prior_sessions(
            Path("unused-backfill-snapshots"),
            date(2026, 8, 10),
            provider,
            prior_sessions=3,
            max_calendar_days=10,
        )
        self.assertEqual([item.market_date for item in sessions], dates)
        self.assertTrue(all(item.needs_write for item in sessions))
        self.assertEqual(sessions[1].snapshot["previous_date"], "2026-08-04")

    def test_cffex_session_ignores_environment_proxy_by_default(self) -> None:
        with patch.dict(os.environ, {"CFFEX_TRUST_ENV": ""}):
            session = create_session()
            try:
                self.assertFalse(session.trust_env)
            finally:
                session.close()
        with patch.dict(os.environ, {"CFFEX_TRUST_ENV": "true"}):
            session = create_session()
            try:
                self.assertTrue(session.trust_env)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
