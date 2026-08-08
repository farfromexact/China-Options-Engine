from __future__ import annotations

import argparse
import json
import re
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from uuid import uuid4

from engine import RISK_FREE_RATE
from eod_enrich import (
    HISTORY_OPTION_PRICE_BASIS,
    SNAPSHOT_DIR,
    TZ_CN,
    add_changes,
    build_official_settlement_products,
    build_radar_summary,
    compact_history_products,
    download_monthly_zip,
    download_single_daily_csv,
    is_verified_snapshot,
    parse_cffex_eod_content,
    parse_option_contract,
)
from futures_link import (
    FUTURE_PRODUCTS,
    build_linkage,
    parse_future_rows_content,
    summarize_product,
)
from radar_history import (
    RADAR_HISTORY_PATH,
    build_history_document,
    build_history_record,
    rebuild_from_snapshots,
    upsert_record,
    write_history,
)


DEFAULT_PRIOR_SESSIONS = 20
DEFAULT_MAX_CALENDAR_DAYS = 60
SETTLEMENT_PRICE_BASIS = HISTORY_OPTION_PRICE_BASIS


class KnownNonTradingDay(FileNotFoundError):
    """The monthly CFFEX archive exists but has no daily file for this date."""


@dataclass
class CollectedSession:
    market_date: date
    snapshot: dict[str, Any]
    needs_write: bool


MonthlyFetcher = Callable[[str], tuple[str, bytes]]
DailyFetcher = Callable[[date], tuple[str, bytes]]


class MonthlyCsvProvider:
    """Read each monthly ZIP at most once and fall back to a single daily file."""

    def __init__(
        self,
        monthly_fetcher: MonthlyFetcher = download_monthly_zip,
        daily_fetcher: DailyFetcher = download_single_daily_csv,
    ) -> None:
        self.monthly_fetcher = monthly_fetcher
        self.daily_fetcher = daily_fetcher
        self._monthly: dict[str, tuple[str, dict[str, tuple[str, bytes]]] | Exception] = {}

    def _load_month(self, yyyymm: str) -> tuple[str, dict[str, tuple[str, bytes]]] | Exception:
        if yyyymm in self._monthly:
            return self._monthly[yyyymm]
        try:
            source, content = self.monthly_fetcher(yyyymm)
            daily: dict[str, tuple[str, bytes]] = {}
            with zipfile.ZipFile(BytesIO(content)) as archive:
                for name in archive.namelist():
                    filename = name.replace("\\", "/").rsplit("/", 1)[-1]
                    match = re.fullmatch(r"(\d{8})_1\.csv", filename)
                    if match:
                        daily[match.group(1)] = (f"{source}#{name}", archive.read(name))
            if not daily:
                raise RuntimeError(f"CFFEX monthly ZIP {yyyymm} contains no daily CSV files")
            loaded: tuple[str, dict[str, tuple[str, bytes]]] | Exception = (source, daily)
        except Exception as exc:
            loaded = exc
        self._monthly[yyyymm] = loaded
        return loaded

    def get(self, trade_date: date) -> tuple[str, bytes]:
        ds = trade_date.strftime("%Y%m%d")
        month = self._load_month(ds[:6])
        if not isinstance(month, Exception):
            _, daily = month
            if ds not in daily:
                raise KnownNonTradingDay(f"{ds}_1.csv is absent from the verified monthly ZIP")
            return daily[ds]

        try:
            return self.daily_fetcher(trade_date)
        except Exception as daily_error:
            raise RuntimeError(
                f"CFFEX data unavailable for weekday {trade_date}: "
                f"monthly ZIP failed ({month}); daily CSV failed ({daily_error})"
            ) from daily_error


def build_option_products(
    official_records: dict[str, dict[str, Any]], market_date: date
) -> dict[str, Any]:
    return build_official_settlement_products(official_records, market_date)


def build_futures_block(
    market_date: date, source: str, content: bytes
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_products, source_status = parse_future_rows_content(market_date, source, content)
    summaries = {
        product: summarize_product(product, raw_products.get(product, []))
        for product in FUTURE_PRODUCTS
    }
    incomplete = [product for product, summary in summaries.items() if summary.get("status") != "ok"]
    if incomplete:
        raise RuntimeError(f"official futures data missing active products: {incomplete}")
    return {
        "trade_date": market_date.isoformat(),
        "source_status": source_status,
        "products": summaries,
    }, summaries


def build_verified_snapshot(
    market_date: date,
    source: str,
    content: bytes,
    previous_date: date | None = None,
) -> dict[str, Any]:
    official, official_status = parse_cffex_eod_content(market_date, source, content)
    products = build_option_products(official, market_date)
    futures_block, futures_summaries = build_futures_block(market_date, source, content)
    generated_at = datetime.combine(market_date, time(15, 30), tzinfo=TZ_CN).isoformat()

    result: dict[str, Any] = {
        "date": market_date.isoformat(),
        "run_date": market_date.isoformat(),
        "generated_at": generated_at,
        "data_fresh": True,
        "source": "CFFEX official daily EOD historical backfill",
        "collection_mode": "historical_eod_backfill",
        "history_record_origin": "historical_eod_backfill",
        "history_option_price_basis": SETTLEMENT_PRICE_BASIS,
        "assumptions": {
            "risk_free_rate": RISK_FREE_RATE,
            "expiry_calendar": "third Friday inferred from YYMM symbol; holiday adjustment not applied",
            "iv_model": "Black-76 on put-call-parity implied forward",
            "iv_price": SETTLEMENT_PRICE_BASIS,
            "historical_limit": "bid/ask quotes and order-book sizes cannot be reconstructed",
        },
        "products": products,
        "history_products": compact_history_products(products),
        "previous_date": previous_date.isoformat() if previous_date else None,
        "errors": [],
        "official_eod_source": source,
        "source_status": {
            "option_chain": "ok",
            "volume": "ok",
            "official_eod": official_status,
            "official_quote_matches": len(official),
            "total_chain_quotes": len(official),
            "official_quote_match_coverage": 1.0,
            "freshness": "fresh",
            "freshness_method": (
                "CFFEX official daily CSV historical backfill; option analytics use "
                "official EOD settlement with positive close fallback"
            ),
        },
        "futures": futures_block,
    }
    result["source_status"]["futures"] = futures_block["source_status"]
    result["futures_option_linkage"] = build_linkage(
        futures_summaries, build_radar_summary(result)
    )
    result["history_futures_option_linkage"] = deepcopy(
        result["futures_option_linkage"]
    )

    if not is_verified_snapshot(result, market_date.isoformat()):
        raise RuntimeError(f"constructed snapshot failed verified-source checks: {market_date}")
    build_history_record(result)
    return result


def load_verified_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return snapshot if is_verified_snapshot(snapshot, path.stem) else None


def has_canonical_settlement_history(snapshot: dict[str, Any]) -> bool:
    if snapshot.get("history_option_price_basis") != SETTLEMENT_PRICE_BASIS:
        return False
    history_products = snapshot.get("history_products")
    if not isinstance(history_products, dict):
        return False
    if any(
        not isinstance(history_products.get(product), dict)
        or not history_products[product].get("expiries")
        for product in ("HO", "IO", "MO")
    ):
        return False
    history_linkage = snapshot.get("history_futures_option_linkage")
    return isinstance(history_linkage, dict) and all(
        isinstance(history_linkage.get(product), dict)
        for product in FUTURE_PRODUCTS
    )


def enrich_anchor_history(
    anchor_snapshot: dict[str, Any],
    settlement_snapshot: dict[str, Any],
    previous_date: date,
) -> dict[str, Any]:
    enriched = deepcopy(anchor_snapshot)
    enriched["history_products"] = deepcopy(settlement_snapshot["history_products"])
    enriched["history_record_origin"] = "scheduled_eod"
    enriched["history_option_price_basis"] = SETTLEMENT_PRICE_BASIS
    enriched["history_futures_option_linkage"] = deepcopy(
        settlement_snapshot["futures_option_linkage"]
    )
    enriched["previous_date"] = previous_date.isoformat()
    build_history_record(enriched)
    return enriched


def find_anchor(snapshot_dir: Path, requested: date | None = None) -> tuple[date, dict[str, Any]]:
    if requested is not None:
        path = snapshot_dir / f"{requested.isoformat()}.json"
        verified = load_verified_snapshot(path) if path.exists() else None
        if verified is None:
            raise RuntimeError(f"anchor snapshot is missing or unverified: {path}")
        return requested, verified

    for path in sorted(snapshot_dir.glob("*.json"), reverse=True):
        verified = load_verified_snapshot(path)
        if verified is not None:
            return date.fromisoformat(path.stem), verified
    raise RuntimeError("no verified snapshot is available as the backfill anchor")


def collect_prior_sessions(
    snapshot_dir: Path,
    anchor_date: date,
    provider: MonthlyCsvProvider,
    *,
    prior_sessions: int = DEFAULT_PRIOR_SESSIONS,
    max_calendar_days: int = DEFAULT_MAX_CALENDAR_DAYS,
    overwrite: bool = False,
) -> list[CollectedSession]:
    if prior_sessions <= 0:
        raise ValueError("prior_sessions must be positive")
    if max_calendar_days < prior_sessions:
        raise ValueError("max_calendar_days must be at least prior_sessions")

    collected: list[CollectedSession] = []
    cursor = anchor_date - timedelta(days=1)
    oldest_allowed = anchor_date - timedelta(days=max_calendar_days)
    while cursor >= oldest_allowed and len(collected) < prior_sessions:
        if cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
            continue

        target = snapshot_dir / f"{cursor.isoformat()}.json"
        if target.exists() and not overwrite:
            existing = load_verified_snapshot(target)
            if existing is None:
                raise RuntimeError(
                    f"refusing to replace an existing unverified snapshot without --overwrite: {target}"
                )
            if not has_canonical_settlement_history(existing):
                raise RuntimeError(
                    "existing snapshot does not use the canonical settlement history "
                    f"schema; rerun with --overwrite: {target}"
                )
            collected.append(CollectedSession(cursor, existing, False))
            cursor -= timedelta(days=1)
            continue

        try:
            source, content = provider.get(cursor)
        except KnownNonTradingDay:
            cursor -= timedelta(days=1)
            continue
        snapshot = build_verified_snapshot(cursor, source, content)
        collected.append(CollectedSession(cursor, snapshot, True))
        cursor -= timedelta(days=1)

    if len(collected) != prior_sessions:
        raise RuntimeError(
            f"found {len(collected)} verified prior sessions, expected {prior_sessions}, "
            f"within {max_calendar_days} calendar days"
        )

    collected.sort(key=lambda item: item.market_date)
    previous: dict[str, Any] | None = None
    for session in collected:
        if session.needs_write:
            add_changes(session.snapshot, previous)
            summaries = session.snapshot["futures"]["products"]
            session.snapshot["futures_option_linkage"] = build_linkage(
                summaries, build_radar_summary(session.snapshot)
            )
            build_history_record(session.snapshot)
        previous = session.snapshot
    return collected


def _stage_snapshots(
    sessions: list[CollectedSession], staging_dir: Path
) -> list[tuple[Path, str]]:
    staged: list[tuple[Path, str]] = []
    for session in sessions:
        if not session.needs_write:
            continue
        filename = f"{session.market_date.isoformat()}.json"
        path = staging_dir / filename
        path.write_text(
            json.dumps(session.snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        if not is_verified_snapshot(reloaded, session.market_date.isoformat()):
            raise RuntimeError(f"staged snapshot failed validation: {filename}")
        build_history_record(reloaded)
        staged.append((path, filename))
    return staged


def _publish_staged(
    staged: list[tuple[Path, str]],
    snapshot_dir: Path,
    *,
    overwrite: bool,
    replace_existing: set[str] | None = None,
    post_publish: Callable[[], None] | None = None,
) -> None:
    replace_existing = replace_existing or set()
    for _, filename in staged:
        target = snapshot_dir / filename
        if target.exists() and not overwrite and filename not in replace_existing:
            raise FileExistsError(f"snapshot appeared during staging: {target}")

    backup_dir = staged[0][0].parent / ".backups" if staged else None
    published: list[tuple[Path, Path | None]] = []
    try:
        for staged_path, filename in staged:
            target = snapshot_dir / filename
            backup: Path | None = None
            if target.exists():
                if not overwrite and filename not in replace_existing:
                    raise FileExistsError(f"snapshot appeared during publish: {target}")
                assert backup_dir is not None
                backup_dir.mkdir(exist_ok=True)
                backup = backup_dir / filename
                target.replace(backup)
            try:
                # A TemporaryDirectory is intentionally private. Moving one of
                # its children on Windows also moves that restrictive ACL, which
                # can make the published snapshot unreadable to later runners.
                # Create the install file beside the target so it inherits the
                # snapshot directory permissions, then atomically replace it.
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(staged_path.read_bytes())
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            except Exception:
                if backup is not None:
                    backup.replace(target)
                raise
            published.append((target, backup))
        if post_publish is not None:
            post_publish()
    except Exception:
        for target, backup in reversed(published):
            if target.exists():
                target.unlink()
            if backup is not None and backup.exists():
                backup.replace(target)
        raise


def run_backfill(
    snapshot_dir: Path = SNAPSHOT_DIR,
    history_path: Path = RADAR_HISTORY_PATH,
    *,
    anchor: date | None = None,
    prior_sessions: int = DEFAULT_PRIOR_SESSIONS,
    max_calendar_days: int = DEFAULT_MAX_CALENDAR_DAYS,
    overwrite: bool = False,
    dry_run: bool = False,
    provider: MonthlyCsvProvider | None = None,
) -> dict[str, Any]:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    anchor_date, anchor_snapshot = find_anchor(snapshot_dir, anchor)
    provider = provider or MonthlyCsvProvider()
    sessions = collect_prior_sessions(
        snapshot_dir,
        anchor_date,
        provider,
        prior_sessions=prior_sessions,
        max_calendar_days=max_calendar_days,
        overwrite=overwrite,
    )

    anchor_source, anchor_content = provider.get(anchor_date)
    settlement_anchor = build_verified_snapshot(
        anchor_date,
        anchor_source,
        anchor_content,
        previous_date=sessions[-1].market_date,
    )
    enriched_anchor = enrich_anchor_history(
        anchor_snapshot,
        settlement_anchor,
        sessions[-1].market_date,
    )
    anchor_session = CollectedSession(anchor_date, enriched_anchor, True)

    replacement_dates = {
        *(session.market_date.isoformat() for session in sessions),
        anchor_date.isoformat(),
    }
    existing_records = rebuild_from_snapshots(
        snapshot_dir, exclude_dates=replacement_dates
    )
    expected_records = existing_records
    for session in sessions:
        expected_records = upsert_record(expected_records, build_history_record(session.snapshot))
    expected_records = upsert_record(expected_records, build_history_record(enriched_anchor))
    expected_document = build_history_document(expected_records)
    minimum_count = prior_sessions + 1
    if expected_document["record_count"] < minimum_count:
        raise RuntimeError(
            f"backfill would produce only {expected_document['record_count']} records; "
            f"at least {minimum_count} are required for a {prior_sessions}-session comparison"
        )

    with TemporaryDirectory(prefix=".cffex-backfill-", dir=snapshot_dir.parent) as raw_staging:
        staged = _stage_snapshots([*sessions, anchor_session], Path(raw_staging))
        if not dry_run:
            def finalize_history() -> None:
                rebuilt = rebuild_from_snapshots(snapshot_dir)
                document = build_history_document(rebuilt)
                if document != expected_document:
                    raise RuntimeError(
                        "published snapshots do not rebuild to the staged history document"
                    )
                write_history(history_path, document)

            _publish_staged(
                staged,
                snapshot_dir,
                overwrite=overwrite,
                replace_existing={f"{anchor_date.isoformat()}.json"},
                post_publish=finalize_history,
            )

    return {
        "status": "dry_run_ok" if dry_run else "updated",
        "anchor_date": anchor_date.isoformat(),
        "prior_sessions": prior_sessions,
        "first_backfill_date": sessions[0].market_date.isoformat(),
        "latest_backfill_date": sessions[-1].market_date.isoformat(),
        "new_snapshots": sum(session.needs_write for session in sessions),
        "existing_snapshots": sum(not session.needs_write for session in sessions),
        "anchor_history_normalized": True,
        "expected_history_records": expected_document["record_count"],
        "pricing_basis": SETTLEMENT_PRICE_BASIS,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill verified CFFEX EOD snapshots before the latest verified anchor."
    )
    parser.add_argument(
        "--anchor",
        type=date.fromisoformat,
        help="existing verified anchor date; defaults to the newest verified snapshot",
    )
    parser.add_argument("--prior-sessions", type=int, default=DEFAULT_PRIOR_SESSIONS)
    parser.add_argument("--max-calendar-days", type=int, default=DEFAULT_MAX_CALENDAR_DAYS)
    parser.add_argument("--snapshots", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument("--history", type=Path, default=RADAR_HISTORY_PATH)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="download, calculate, stage, and validate without publishing files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_backfill(
        args.snapshots,
        args.history,
        anchor=args.anchor,
        prior_sessions=args.prior_sessions,
        max_calendar_days=args.max_calendar_days,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
