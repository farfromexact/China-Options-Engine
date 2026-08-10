from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.1"
DEFAULT_RETENTION_SESSIONS = int(os.getenv("RADAR_HISTORY_RETENTION", "60"))
DATA_DIR = Path("data")
RADAR_HISTORY_PATH = DATA_DIR / "radar_history.json"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

OPTION_PRODUCTS = ("HO", "IO", "MO")
FUTURE_PRODUCTS = ("IH", "IF", "IC", "IM")

OPTION_METRIC_KEYS = (
    "atm_iv",
    "call25_iv",
    "put25_iv",
    "call10_iv",
    "put10_iv",
    "rr25",
    "bf25",
    "call_oi",
    "put_oi",
    "pcr_oi",
    "call_volume",
    "put_volume",
    "pcr_volume",
    "call_oi_change",
    "put_oi_change",
    "volume_oi_coverage",
)

FUTURE_CONTRACT_KEYS = (
    "symbol",
    "contract_month",
    "expiry_inferred",
    "dte_calendar",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "pre_settle",
    "change_points",
    "change_pct",
    "volume",
    "open_interest",
    "oi_change",
    "oi_change_pct",
)

# Preserve the original 1.1 summary schema for backwards-compatible validation
# of already-committed history. New rebuilds add the cash-index / ETF fields
# below; older records remain valid until the next deterministic snapshot rebuild.
FUTURE_SUMMARY_BASE_KEYS = (
    "next_minus_main_points",
    "annualized_roll_pct_inferred",
    "term_structure_signal",
    "total_volume",
    "total_open_interest",
    "total_oi_change",
    "main_volume_share",
    "main_oi_share",
)

FUTURE_CASH_MARKET_KEYS = (
    "cash_index_code",
    "cash_index_name",
    "cash_index_close",
    "cash_index_change_pct",
    "cash_index_turnover_cny",
    "cash_basis_points",
    "cash_basis_pct",
    "annualized_cash_basis_pct_inferred",
    "cash_basis_note",
    "reference_etf_code",
    "reference_etf_close",
    "reference_etf_total_shares",
    "reference_etf_previous_total_shares",
    "reference_etf_share_change",
    "reference_etf_share_change_pct",
    "reference_etf_estimated_net_creation_redemption_cny",
    "reference_etf_flow_method",
)

FUTURE_SUMMARY_KEYS = FUTURE_SUMMARY_BASE_KEYS + FUTURE_CASH_MARKET_KEYS

LINKAGE_KEYS = (
    "direct_option_product",
    "direct_option_available",
    "matched_option_symbol",
    "matched_option_expiry",
    "option_forward",
    "future_minus_option_forward_points",
    "future_minus_option_forward_pct",
    "linkage_status",
    "proxy_option_products",
    "proxy_warning",
)

QUALITY_KEYS = (
    "record_origin",
    "option_price_basis",
    "option_chain",
    "volume",
    "official_eod_status",
    "freshness",
    "freshness_method",
    "official_quote_matches",
    "total_chain_quotes",
    "official_quote_match_coverage",
    "futures_status",
    "futures_records",
    "errors",
)

RECORD_KEYS = (
    "date",
    "generated_at",
    "data_fresh",
    "previous_date",
    "data_quality",
    "options",
    "futures",
    "futures_option_linkage",
)


class UnverifiedSnapshotError(ValueError):
    """A dated snapshot is real JSON but not eligible for verified history."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_scalar(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _copy_fields(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    item = _mapping(source)
    return {key: _json_scalar(item.get(key)) for key in keys}


def _validated_date(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("radar date must be an ISO date string")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"date must use YYYY-MM-DD format: {value}")
    return value


def _validate_trade_date(value: Any, market_date: str, label: str) -> None:
    if not value or str(value).replace("-", "") != market_date.replace("-", ""):
        raise ValueError(f"{label} trade date does not match radar date")


def _validate_verified_sources(source: Mapping[str, Any], market_date: str) -> None:
    source_status = _mapping(source.get("source_status"))
    freshness = source_status.get("freshness")
    if freshness != "fresh":
        raise UnverifiedSnapshotError(
            f"verified history snapshot has non-fresh source status: {freshness}"
        )
    if source_status.get("option_chain") != "ok" or source_status.get("volume") != "ok":
        raise UnverifiedSnapshotError(
            "verified history snapshot is missing complete option-chain EOD data"
        )
    coverage = source_status.get("official_quote_match_coverage")
    if not isinstance(coverage, (int, float)) or not math.isclose(
        float(coverage), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise UnverifiedSnapshotError(
            f"verified history snapshot has incomplete official option coverage: {coverage}"
        )

    raw_official = source_status.get("official_eod")
    if not isinstance(raw_official, Mapping):
        raise UnverifiedSnapshotError("verified history snapshot is missing official EOD status")
    if raw_official.get("status") != "ok":
        raise UnverifiedSnapshotError(
            f"verified history snapshot has invalid official EOD status: {raw_official.get('status')}"
        )
    if not raw_official.get("trade_date"):
        raise UnverifiedSnapshotError("verified history snapshot is missing official EOD trade date")
    _validate_trade_date(raw_official.get("trade_date"), market_date, "official EOD")

    futures_status = _mapping(_mapping(source.get("futures")).get("source_status"))
    if futures_status.get("status") != "ok":
        raise UnverifiedSnapshotError("verified history snapshot is missing successful futures data")
    if not futures_status.get("trade_date"):
        raise UnverifiedSnapshotError("verified history snapshot is missing futures trade date")
    _validate_trade_date(futures_status.get("trade_date"), market_date, "futures")

    option_products = source.get("history_products") or source.get("products")
    if not isinstance(option_products, Mapping):
        raise UnverifiedSnapshotError("verified history snapshot has no option products")
    for product in OPTION_PRODUCTS:
        payload = option_products.get(product)
        expiries = _mapping(payload).get("expiries")
        if not isinstance(expiries, list) or not expiries:
            raise UnverifiedSnapshotError(
                f"verified history snapshot has no active {product} expiries"
            )

    futures_products = _mapping(_mapping(source.get("futures")).get("products"))
    for product in FUTURE_PRODUCTS:
        summary = _mapping(futures_products.get(product))
        main_contract = _mapping(summary.get("main_contract"))
        if summary.get("status") != "ok" or not main_contract.get("symbol"):
            raise UnverifiedSnapshotError(
                f"verified history snapshot has incomplete {product} futures data"
            )


def _compact_gamma_peaks(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    peaks = metrics.get("gamma_peaks")
    if not isinstance(peaks, list):
        return compact
    for raw_peak in peaks[:3]:
        peak = _mapping(raw_peak)
        compact.append(
            {
                "strike": _json_scalar(peak.get("strike")),
                "abs_gamma_1pct": _json_scalar(peak.get("abs_gamma_1pct")),
            }
        )
    return compact


def _calendar_dte(market_date: str, expiry: Any) -> int | None:
    if not isinstance(expiry, str):
        return None
    try:
        return (date.fromisoformat(expiry) - date.fromisoformat(market_date)).days
    except ValueError:
        return None


def _compact_options(source: Mapping[str, Any], market_date: str) -> dict[str, Any]:
    products: dict[str, Any] = {}
    history_products = source.get("history_products")
    source_products = (
        history_products
        if isinstance(history_products, Mapping) and history_products
        else _mapping(source.get("products"))
    )
    for product in OPTION_PRODUCTS:
        raw_product = source_products.get(product)
        expiries: list[dict[str, Any]] = []
        raw_expiries = _mapping(raw_product).get("expiries")
        if not isinstance(raw_expiries, list):
            raw_expiries = []
        ordered = sorted(
            (item for item in raw_expiries if isinstance(item, Mapping)),
            key=lambda item: str(item.get("expiry") or "9999-12-31"),
        )
        for tenor_rank, expiry in enumerate(ordered[:4], start=1):
            metrics = _mapping(expiry.get("metrics"))
            metric_values = {key: _json_scalar(metrics.get(key)) for key in OPTION_METRIC_KEYS}
            metric_values["gamma_peaks"] = _compact_gamma_peaks(metrics)
            expiries.append(
                {
                    "tenor_rank": tenor_rank,
                    "symbol": expiry.get("symbol"),
                    "expiry": expiry.get("expiry"),
                    "dte_calendar": _calendar_dte(market_date, expiry.get("expiry")),
                    "forward": _json_scalar(expiry.get("forward")),
                    "metrics": metric_values,
                }
            )
        products[product] = {"expiries": expiries}
    return products


def _compact_futures(source: Mapping[str, Any]) -> dict[str, Any]:
    futures = _mapping(source.get("futures"))
    compact: dict[str, Any] = {}
    source_products = _mapping(futures.get("products"))
    for product in FUTURE_PRODUCTS:
        raw_summary = source_products.get(product)
        summary = _mapping(raw_summary)
        compact[product] = {
            "main_contract": _copy_fields(summary.get("main_contract"), FUTURE_CONTRACT_KEYS),
            "next_contract": _copy_fields(summary.get("next_contract"), FUTURE_CONTRACT_KEYS),
            **_copy_fields(summary, FUTURE_SUMMARY_KEYS),
        }
    return compact


def _compact_linkage(source: Mapping[str, Any]) -> dict[str, Any]:
    history_products = source.get("history_products")
    if isinstance(history_products, Mapping) and history_products:
        raw_history_linkage = source.get("history_futures_option_linkage")
        if not isinstance(raw_history_linkage, Mapping):
            raise ValueError(
                "settlement-based history products require settlement-based futures linkage"
            )
        linkages = raw_history_linkage
    else:
        linkages = _mapping(source.get("futures_option_linkage"))
    return {product: _copy_fields(linkages.get(product), LINKAGE_KEYS) for product in FUTURE_PRODUCTS}


def _compact_quality(source: Mapping[str, Any]) -> dict[str, Any]:
    status = _mapping(source.get("source_status"))
    official = _mapping(status.get("official_eod"))
    futures_status = _mapping(_mapping(source.get("futures")).get("source_status"))
    assumptions = _mapping(source.get("assumptions"))
    errors = source.get("errors")
    return {
        "record_origin": (
            source.get("history_record_origin")
            or source.get("collection_mode")
            or "scheduled_eod"
        ),
        "option_price_basis": (
            source.get("history_option_price_basis")
            or source.get("option_price_basis")
            or assumptions.get("iv_price")
            or "sina_bid_ask_mid"
        ),
        "option_chain": status.get("option_chain"),
        "volume": status.get("volume"),
        "official_eod_status": official.get("status"),
        "freshness": status.get("freshness"),
        "freshness_method": status.get("freshness_method"),
        "official_quote_matches": _json_scalar(status.get("official_quote_matches")),
        "total_chain_quotes": _json_scalar(status.get("total_chain_quotes")),
        "official_quote_match_coverage": _json_scalar(status.get("official_quote_match_coverage")),
        "futures_status": futures_status.get("status"),
        "futures_records": _json_scalar(futures_status.get("records")),
        "errors": errors if isinstance(errors, list) else [],
    }


def build_history_record(source: Mapping[str, Any]) -> dict[str, Any]:
    if source.get("data_fresh") is not True:
        raise ValueError("only data_fresh=true snapshots may enter radar history")
    market_date = _validated_date(source.get("date"))
    _validate_verified_sources(source, market_date)
    record = {
        "date": market_date,
        "generated_at": source.get("generated_at"),
        "data_fresh": True,
        "previous_date": source.get("previous_date"),
        "data_quality": _compact_quality(source),
        "options": _compact_options(source, market_date),
        "futures": _compact_futures(source),
        "futures_option_linkage": _compact_linkage(source),
    }
    return _validate_record(record)


def _require_exact_keys(value: Any, expected: tuple[str, ...], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if set(value) != set(expected):
        raise ValueError(f"{label} has unexpected schema")
    return value


def _require_legacy_or_current_future_summary(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    legacy_keys = {"main_contract", "next_contract", *FUTURE_SUMMARY_BASE_KEYS}
    current_keys = {"main_contract", "next_contract", *FUTURE_SUMMARY_KEYS}
    actual = set(value)
    if actual != legacy_keys and actual != current_keys:
        raise ValueError(f"{label} has unexpected schema")
    return value


def _validate_finite_values(value: Any, label: str = "record") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_finite_values(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_finite_values(item, f"{label}[{index}]")


def _validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("history records must be objects")
    _require_exact_keys(record, RECORD_KEYS, "history record")
    market_date = _validated_date(record.get("date"))
    if record.get("data_fresh") is not True:
        raise ValueError(f"history record {record.get('date')} is not verified fresh data")
    if not isinstance(record.get("generated_at"), str):
        raise ValueError(f"history record {market_date} has invalid generated_at")
    datetime.fromisoformat(record["generated_at"])
    previous_date = record.get("previous_date")
    if previous_date is not None and _validated_date(previous_date) >= market_date:
        raise ValueError(f"history record {market_date} has invalid previous_date")

    quality = _require_exact_keys(record.get("data_quality"), QUALITY_KEYS, "data_quality")
    if not isinstance(quality.get("record_origin"), str) or not isinstance(
        quality.get("option_price_basis"), str
    ):
        raise ValueError(f"history record {market_date} has invalid pricing provenance")
    if (
        quality.get("freshness") != "fresh"
        or quality.get("official_eod_status") != "ok"
        or quality.get("futures_status") != "ok"
    ):
        raise ValueError(f"history record {market_date} lacks verified official sources")
    if not isinstance(quality.get("errors"), list):
        raise ValueError(f"history record {market_date} has invalid errors")

    options = _require_exact_keys(record.get("options"), OPTION_PRODUCTS, "options")
    for product in OPTION_PRODUCTS:
        product_data = _require_exact_keys(options[product], ("expiries",), f"options.{product}")
        expiries = product_data.get("expiries")
        if not isinstance(expiries, list) or len(expiries) > 4:
            raise ValueError(f"options.{product}.expiries must contain at most four items")
        for rank, expiry in enumerate(expiries, start=1):
            expiry_data = _require_exact_keys(
                expiry,
                ("tenor_rank", "symbol", "expiry", "dte_calendar", "forward", "metrics"),
                f"options.{product}.expiries[{rank - 1}]",
            )
            if expiry_data.get("tenor_rank") != rank or not isinstance(expiry_data.get("symbol"), str):
                raise ValueError(f"options.{product}.expiries[{rank - 1}] has invalid identity")
            expiry_date = _validated_date(expiry_data.get("expiry"))
            if expiry_data.get("dte_calendar") != (date.fromisoformat(expiry_date) - date.fromisoformat(market_date)).days:
                raise ValueError(f"options.{product}.expiries[{rank - 1}] has invalid dte_calendar")
            metrics = _require_exact_keys(
                expiry_data.get("metrics"), OPTION_METRIC_KEYS + ("gamma_peaks",), "option metrics"
            )
            peaks = metrics.get("gamma_peaks")
            if not isinstance(peaks, list) or len(peaks) > 3:
                raise ValueError("option gamma_peaks must contain at most three items")
            for peak in peaks:
                _require_exact_keys(peak, ("strike", "abs_gamma_1pct"), "option gamma peak")

    futures = _require_exact_keys(record.get("futures"), FUTURE_PRODUCTS, "futures")
    for product in FUTURE_PRODUCTS:
        summary = _require_legacy_or_current_future_summary(
            futures[product], f"futures.{product}"
        )
        for contract_name in ("main_contract", "next_contract"):
            _require_exact_keys(summary.get(contract_name), FUTURE_CONTRACT_KEYS, f"futures.{product}.{contract_name}")

    linkages = _require_exact_keys(
        record.get("futures_option_linkage"), FUTURE_PRODUCTS, "futures_option_linkage"
    )
    for product in FUTURE_PRODUCTS:
        _require_exact_keys(linkages[product], LINKAGE_KEYS, f"futures_option_linkage.{product}")

    _validate_finite_values(record)
    return record


def load_history_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("radar history must be a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported radar history schema: {document.get('schema_version')}")
    records = document.get("records")
    if not isinstance(records, list):
        raise ValueError("radar history records must be an array")
    return [_validate_record(record) for record in records]


def upsert_record(records: list[dict[str, Any]], new_record: dict[str, Any]) -> list[dict[str, Any]]:
    validated = _validate_record(new_record)
    by_date = {_validated_date(record.get("date")): _validate_record(record) for record in records}
    by_date[validated["date"]] = validated
    return [by_date[key] for key in sorted(by_date)]


def build_history_document(
    records: list[dict[str, Any]], retention_sessions: int = DEFAULT_RETENTION_SESSIONS
) -> dict[str, Any]:
    if retention_sessions <= 0:
        raise ValueError("retention_sessions must be positive")
    by_date: dict[str, dict[str, Any]] = {}
    for record in records:
        validated = _validate_record(record)
        by_date[validated["date"]] = validated
    retained = [by_date[key] for key in sorted(by_date)][-retention_sessions:]
    normalized: list[dict[str, Any]] = []
    for record in retained:
        normalized_record = {
            **record,
            "previous_date": normalized[-1]["date"] if normalized else None,
        }
        normalized.append(_validate_record(normalized_record))
    latest = normalized[-1] if normalized else None
    return {
        "schema_version": SCHEMA_VERSION,
        "description": "Compact verified China index futures/options history for radar automation and dashboards.",
        "retention_sessions": retention_sessions,
        "record_count": len(normalized),
        "first_date": normalized[0]["date"] if normalized else None,
        "latest_date": latest["date"] if latest else None,
        "updated_at": latest.get("generated_at") if latest else None,
        "records": normalized,
    }


def write_history(path: Path, document: dict[str, Any]) -> bool:
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)
    return True


def rebuild_from_snapshots(
    snapshot_dir: Path,
    retention_sessions: int = DEFAULT_RETENTION_SESSIONS,
    *,
    exclude_dates: set[str] | None = None,
) -> list[dict[str, Any]]:
    if retention_sessions <= 0:
        raise ValueError("retention_sessions must be positive")
    excluded = exclude_dates or set()
    by_date: dict[str, dict[str, Any]] = {}
    for path in sorted(snapshot_dir.glob("*.json"), reverse=True):
        if path.stem in excluded:
            continue
        if len(by_date) >= retention_sessions:
            break
        source = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(source, Mapping):
            raise ValueError(f"snapshot must contain a JSON object: {path}")
        if source.get("data_fresh") is not True:
            continue
        market_date = _validated_date(source.get("date"))
        if market_date != path.stem:
            raise ValueError(f"snapshot filename/date mismatch: {path}")
        try:
            record = build_history_record(source)
        except UnverifiedSnapshotError:
            # Legacy/pre-EOD files remain useful audit artifacts but must not
            # block rebuilding the verified, fixed-size consumer history.
            continue
        by_date[market_date] = record
    return [by_date[key] for key in sorted(by_date)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build compact radar history from verified China options data.")
    parser.add_argument("--check", action="store_true", help="validate the committed history without changing it")
    parser.add_argument("--history", type=Path, default=RADAR_HISTORY_PATH)
    parser.add_argument("--snapshots", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument("--retention", type=int, default=DEFAULT_RETENTION_SESSIONS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.check:
        records = load_history_records(args.history)
        document = build_history_document(records, args.retention)
        if json.loads(args.history.read_text(encoding="utf-8")) != document:
            raise ValueError("radar history metadata, ordering, or deduplication is inconsistent")
        print(json.dumps({"status": "ok", "record_count": len(records), "latest_date": document["latest_date"]}))
        return

    records = rebuild_from_snapshots(args.snapshots, args.retention)
    if not records:
        raise RuntimeError("no verified snapshots are available to rebuild radar history")

    document = build_history_document(records, args.retention)
    changed = write_history(args.history, document)
    print(
        json.dumps(
            {
                "status": "updated" if changed else "unchanged",
                "record_count": document["record_count"],
                "first_date": document["first_date"],
                "latest_date": document["latest_date"],
            }
        )
    )


if __name__ == "__main__":
    main()
