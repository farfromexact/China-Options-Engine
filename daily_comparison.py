"""Use a verified prior session and comparable observations for daily changes."""
from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any

from chinese_calendar import get_holiday_detail

MIN_OFFICIAL_CHAIN_COVERAGE = 0.95


def verified_option_eod(snapshot: Any, day: str) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("date") != day or snapshot.get("data_fresh") is not True:
        return False
    status = snapshot.get("source_status")
    if not isinstance(status, dict) or status.get("freshness") != "fresh":
        return False
    official = status.get("official_eod")
    coverage = status.get("official_quote_match_coverage")
    return (status.get("option_chain") == "ok" and status.get("volume") == "ok"
            and isinstance(coverage, (int, float)) and MIN_OFFICIAL_CHAIN_COVERAGE <= coverage <= 1
            and isinstance(official, dict) and official.get("status") == "ok"
            and str(official.get("trade_date") or "").replace("-", "") == day.replace("-", ""))


def prior_session(day: date) -> date | None:
    try:
        if day.weekday() >= 5 or get_holiday_detail(day)[0]:
            return None
        day -= timedelta(days=1)
        while day.weekday() >= 5 or get_holiday_detail(day)[0]:
            day -= timedelta(days=1)
        return day
    except NotImplementedError:
        return None


def previous_snapshot(directory: Path, day: date) -> dict | None:
    prior = prior_session(day)
    if prior is None:
        return None
    path = directory / f"{prior.isoformat()}.json"
    if not path.is_file():
        return None
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not verified_option_eod(snapshot, prior.isoformat()):
        return None
    return snapshot


def add_changes(current: dict[str, Any], previous: dict[str, Any] | None) -> None:
    prior = prior_session(date.fromisoformat(current["date"]))
    if current.get("data_fresh") is not True or not prior or not verified_option_eod(previous, prior.isoformat()):
        previous = {}
    current["previous_date"] = previous.get("date")
    basis = current.get("assumptions", {}).get("iv_price")
    comparable_iv = bool(basis and basis == previous.get("assumptions", {}).get("iv_price"))
    for product, data in current["products"].items():
        old_expiries = {e["symbol"]: e for e in previous.get("products", {}).get(product, {}).get("expiries", [])}
        for expiry in data.get("expiries", []):
            metrics = expiry["metrics"]
            # The intraday collector may have populated these for a provisional
            # date. Always discard them before applying the verified EOD date.
            for name in list(metrics):
                if name.endswith("_change_1d"):
                    del metrics[name]
            old = old_expiries.get(expiry["symbol"], {}).get("metrics", {})
            for name in ("atm_iv", "rr25", "pcr_oi", "call_oi", "put_oi", "pcr_volume",
                         "pcr_volume_partial", "call_volume", "put_volume"):
                if name in ("atm_iv", "rr25") and not comparable_iv:
                    continue
                if metrics.get(name) is not None and old.get(name) is not None:
                    metrics[f"{name}_change_1d"] = metrics[name] - old[name]
            new_peak = (metrics.get("gamma_peaks") or [{}])[0].get("strike")
            old_peak = (old.get("gamma_peaks") or [{}])[0].get("strike")
            if comparable_iv and new_peak is not None and old_peak is not None:
                metrics["gamma_peak_change_1d"] = new_peak - old_peak
