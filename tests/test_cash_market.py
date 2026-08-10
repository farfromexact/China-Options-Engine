from __future__ import annotations

import unittest

from cash_market import (
    enrich_futures_with_cash_market,
    parse_eastmoney_share_payload,
    parse_tencent_quote_text,
)


class CashMarketTests(unittest.TestCase):
    def test_parse_tencent_quote_text(self) -> None:
        values = [""] * 55
        values[1] = "沪深300"
        values[2] = "000300"
        values[3] = "4650.25"
        values[4] = "4600.00"
        values[5] = "4610.00"
        values[30] = "20260810150000"
        values[31] = "50.25"
        values[32] = "1.09"
        values[33] = "4660.00"
        values[34] = "4595.00"
        values[37] = "1234567.89"
        text = 'v_sh000300="' + "~".join(values) + '";'

        parsed = parse_tencent_quote_text(text)["sh000300"]
        self.assertEqual(parsed["code"], "000300")
        self.assertAlmostEqual(parsed["close"], 4650.25)
        self.assertAlmostEqual(parsed["previous_close"], 4600.0)
        self.assertAlmostEqual(parsed["change_pct"], 0.0109)
        self.assertAlmostEqual(parsed["turnover_cny"], 1234567.89 * 10000.0)

    def test_parse_eastmoney_total_shares(self) -> None:
        parsed = parse_eastmoney_share_payload(
            {
                "data": {
                    "f57": "510300",
                    "f58": "300ETF",
                    "f84": 12345678900,
                    "f85": 12345678900,
                    "f116": 50000000000,
                    "f117": 50000000000,
                    "f124": 1786354800,
                }
            }
        )
        self.assertEqual(parsed["share_field"], "f84_total_shares")
        self.assertEqual(parsed["total_shares"], 12345678900)

    def test_parse_eastmoney_falls_back_to_float_shares(self) -> None:
        parsed = parse_eastmoney_share_payload(
            {"data": {"f57": "510050", "f84": None, "f85": 1000000000}}
        )
        self.assertEqual(parsed["share_field"], "f85_float_shares_fallback")
        self.assertEqual(parsed["total_shares"], 1000000000)

    def test_enrich_futures_with_true_cash_basis_and_etf_flow(self) -> None:
        futures = {
            "IF": {
                "main_contract": {
                    "symbol": "IF2609",
                    "close": 4644.6,
                    "dte_calendar": 39,
                }
            }
        }
        cash_market = {
            "indices": {
                "000300": {
                    "close": 4660.0,
                    "change_pct": 0.005,
                    "turnover_cny": 250000000000.0,
                }
            },
            "reference_etfs": {
                "510300": {
                    "close": 4.66,
                    "total_shares": 10000000000,
                    "previous_total_shares": 9800000000,
                    "share_change": 200000000,
                    "share_change_pct": 200000000 / 9800000000,
                    "estimated_net_creation_redemption_cny": 932000000.0,
                    "flow_method": "daily total-share change * ETF market close",
                }
            },
        }

        enrich_futures_with_cash_market(futures, cash_market)
        summary = futures["IF"]
        self.assertAlmostEqual(summary["cash_basis_points"], -15.4)
        self.assertAlmostEqual(summary["cash_basis_pct"], -15.4 / 4660.0)
        self.assertAlmostEqual(
            summary["annualized_cash_basis_pct_inferred"],
            (-15.4 / 4660.0) * 365.0 / 39.0,
        )
        self.assertEqual(summary["reference_etf_code"], "510300")
        self.assertEqual(summary["reference_etf_share_change"], 200000000)
        self.assertEqual(
            summary["reference_etf_estimated_net_creation_redemption_cny"],
            932000000.0,
        )


if __name__ == "__main__":
    unittest.main()
