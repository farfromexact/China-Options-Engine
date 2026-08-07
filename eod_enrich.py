from __future__ import annotations

import csv
import json
import math
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import requests

TZ_CN = timezone(timedelta(hours=8))
REQ_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 China-Options-Engine/2.0"}

DATA_DIR = Path("data")
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LATEST_PATH = DATA_DIR / "latest.json"
RADAR_LATEST_PATH = DATA_DIR / "radar_latest.json"
STATUS_PATH = DATA_DIR / "last_run_status.json"

CFFEX_MONTHLY_ZIP = "https://www.cffex.com.cn/sj/historysj/{yyyymm}/zip/{yyyymm}.zip"


def parse_num(value: Any) -> float | None:
    try:
        if value in (None, "", "--"):
            return None
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(symbol)).upper()


def fetch_cffex_eod(trade_date: date) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    ds = trade_date.strftime("%Y%m%d")
    yyyymm = ds[:6]
    urls = [
        CFFEX_MONTHLY_ZIP.format(yyyymm=yyyymm),
        CFFEX_MONTHLY_ZIP.format(yyyymm=yyyymm).replace("https://", "http://"),
    ]
    last_error: str | None = None

    for url in urls:
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
            response.raise_for_status()
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                target = next(
                    (
                        name
                        for name in archive.namelist()
                        if name.replace("\\", "/").endswith(f"/{ds}_1.csv")
                        or name == f"{ds}_1.csv"
                    ),
                    None,
                )
                if target is None:
                    raise FileNotFoundError(f"{ds}_1.csv not found in {yyyymm}.zip")
                raw = archive.read(target)

            text = None
            used_encoding = None
            for encoding in ("gb2312", "gbk", "utf-8-sig"):
                try:
                    text = raw.decode(encoding)
                    used_encoding = encoding
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                raise RuntimeError("unable to decode CFFEX CSV")

            reader = csv.reader(StringIO(text))
            header = next(reader, None)
            if not header:
                raise RuntimeError("empty CFFEX CSV")

            records: dict[str, dict[str, Any]] = {}
            samples: list[str] = []
            for row in reader:
                if len(row) < 11:
                    continue
                raw_symbol = row[0].strip()
                if not raw_symbol or raw_symbol in {"小计", "合计"}:
                    continue
                normalized = normalize_symbol(raw_symbol)
                if not normalized.startswith(("HO", "IO", "MO")):
                    continue
                if len(samples) < 10:
                    samples.append(raw_symbol)
                if not re.match(r"^(HO|IO|MO)\d{4}[CP]\d+", normalized):
                    continue
                records[normalized] = {
                    "raw_symbol": raw_symbol,
                    "volume": parse_num(row[4]),
                    "open_interest": parse_num(row[6]),
                    "oi_change": parse_num(row[7]) if len(row) > 7 else None,
                    "close": parse_num(row[8]) if len(row) > 8 else None,
                    "settle": parse_num(row[9]) if len(row) > 9 else None,
                    "pre_settle": parse_num(row[10]) if len(row) > 10 else None,
                }

            if not records:
                raise RuntimeError(
                    f"CFFEX CSV contained no normalized option rows; samples={samples}"
                )

            return records, {
                "status": "ok",
                "url": url,
                "trade_date": ds,
                "records": len(records),
                "encoding": used_encoding,
                "header": header,
                "sample_symbols": samples,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

    return {}, {
        "status": "missing",
        "trade_date": ds,
        "error": last_error or "unknown CFFEX EOD error",
    }


def enrich_quote(quote: dict[str, Any], official: dict[str, Any], forward: float | None) -> None:
    official_oi = official.get("open_interest")
    quote["volume"] = official.get("volume")
    quote["oi_change"] = official.get("oi_change")
    quote["official_oi"] = official_oi
    quote["close_eod"] = official.get("close")
    quote["settle_eod"] = official.get("settle")
    quote["pre_settle_eod"] = official.get("pre_settle")
    if official_oi is not None:
        quote["oi"] = official_oi

    gamma = quote.get("gamma")
    oi = quote.get("oi")
    if gamma is not None and oi is not None:
        quote["gamma_oi"] = gamma * oi * 100.0
        if forward:
            quote["gamma_1pct"] = gamma * oi * 100.0 * forward * forward * 0.01


def recompute_metrics(expiry: dict[str, Any]) -> None:
    rows = expiry.get("rows", [])
    metrics = expiry.setdefault("metrics", {})

    call_oi = sum((row["call"].get("oi") or 0.0) for row in rows)
    put_oi = sum((row["put"].get("oi") or 0.0) for row in rows)
    call_volume = sum((row["call"].get("volume") or 0.0) for row in rows)
    put_volume = sum((row["put"].get("volume") or 0.0) for row in rows)
    call_oi_change = sum((row["call"].get("oi_change") or 0.0) for row in rows)
    put_oi_change = sum((row["put"].get("oi_change") or 0.0) for row in rows)

    total_oi = call_oi + put_oi
    covered_oi = sum(
        (row[side].get("oi") or 0.0)
        for row in rows
        for side in ("call", "put")
        if row[side].get("volume") is not None
    )

    gamma_peaks = []
    for row in rows:
        exposure = abs(row["call"].get("gamma_1pct") or 0.0) + abs(
            row["put"].get("gamma_1pct") or 0.0
        )
        gamma_peaks.append(
            {"strike": row.get("strike"), "abs_gamma_1pct": exposure}
        )
    gamma_peaks.sort(key=lambda x: x["abs_gamma_1pct"], reverse=True)

    metrics.update(
        {
            "call_oi": call_oi,
            "put_oi": put_oi,
            "pcr_oi": put_oi / call_oi if call_oi > 0 else None,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "pcr_volume": put_volume / call_volume if call_volume > 0 else None,
            "call_oi_change": call_oi_change,
            "put_oi_change": put_oi_change,
            "volume_oi_coverage": covered_oi / total_oi if total_oi > 0 else None,
            "gamma_peaks": gamma_peaks[:5],
            "call_volume_partial": call_volume,
            "put_volume_partial": put_volume,
            "pcr_volume_partial": put_volume / call_volume if call_volume > 0 else None,
            "volume_scope": "CFFEX official EOD, full listed option chain for this expiry",
        }
    )


def load_previous(market_date: date) -> dict[str, Any] | None:
    for path in sorted(SNAPSHOT_DIR.glob("*.json"), reverse=True):
        if path.stem < market_date.isoformat():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


def add_changes(current: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if not previous:
        current["previous_date"] = None
        return

    current["previous_date"] = previous.get("date")
    old_products = previous.get("products", {})
    for product, pdata in current.get("products", {}).items():
        old_expiries = {
            item.get("symbol"): item
            for item in old_products.get(product, {}).get("expiries", [])
        }
        for expiry in pdata.get("expiries", []):
            old = old_expiries.get(expiry.get("symbol"))
            if not old:
                continue
            metrics = expiry.get("metrics", {})
            old_metrics = old.get("metrics", {})

            for key in (
                "atm_iv",
                "rr25",
                "pcr_oi",
                "call_oi",
                "put_oi",
                "pcr_volume",
                "call_volume",
                "put_volume",
                "call_oi_change",
                "put_oi_change",
            ):
                old_value = old_metrics.get(key)
                if old_value is None and key == "pcr_volume":
                    old_value = old_metrics.get("pcr_volume_partial")
                if metrics.get(key) is not None and old_value is not None:
                    metrics[f"{key}_change_1d"] = metrics[key] - old_value

            new_peak = (metrics.get("gamma_peaks") or [{}])[0].get("strike")
            old_peak = (old_metrics.get("gamma_peaks") or [{}])[0].get("strike")
            if new_peak is not None and old_peak is not None:
                metrics["gamma_peak_change_1d"] = new_peak - old_peak


def build_radar_summary(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
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
        "gamma_peaks",
        "atm_iv_change_1d",
        "rr25_change_1d",
        "pcr_oi_change_1d",
        "call_oi_change_1d",
        "put_oi_change_1d",
        "pcr_volume_change_1d",
        "call_volume_change_1d",
        "put_volume_change_1d",
        "gamma_peak_change_1d",
    ]

    compact: dict[str, Any] = {
        "date": result.get("date"),
        "generated_at": result.get("generated_at"),
        "data_fresh": result.get("data_fresh"),
        "source_status": result.get("source_status"),
        "previous_date": result.get("previous_date"),
        "products": {},
        "errors": result.get("errors", []),
    }

    for product, pdata in result.get("products", {}).items():
        ordered = sorted(
            pdata.get("expiries", []),
            key=lambda x: x.get("expiry") or "9999-12-31",
        )
        compact["products"][product] = {
            "expiries": [
                {
                    "symbol": expiry.get("symbol"),
                    "expiry": expiry.get("expiry"),
                    "forward": expiry.get("forward"),
                    "metrics": {
                        key: expiry.get("metrics", {}).get(key)
                        for key in keys
                        if key in expiry.get("metrics", {})
                    },
                }
                for expiry in ordered[:4]
            ]
        }
    return compact


def restore_latest_verified() -> dict[str, Any] | None:
    snapshots = sorted(SNAPSHOT_DIR.glob("*.json"), reverse=True)
    if not snapshots:
        return None
    try:
        verified = json.loads(snapshots[0].read_text(encoding="utf-8"))
        LATEST_PATH.write_text(
            json.dumps(verified, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        RADAR_LATEST_PATH.write_text(
            json.dumps(build_radar_summary(verified), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return verified
    except Exception:
        return None


def main() -> None:
    now = datetime.now(TZ_CN)
    run_date = now.date()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    if not LATEST_PATH.exists():
        raise FileNotFoundError("data/latest.json missing after engine.py")

    result = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    eod, status = fetch_cffex_eod(run_date)

    if status.get("status") != "ok":
        restored = restore_latest_verified()
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "run_date": run_date.isoformat(),
                    "generated_at": now.isoformat(),
                    "data_fresh": False,
                    "cffex_eod": status,
                    "restored_latest_date": restored.get("date") if restored else None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "data_fresh": False,
                    "cffex_eod": status,
                    "restored_latest_date": restored.get("date") if restored else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    matched = 0
    total_quotes = 0
    for pdata in result.get("products", {}).values():
        pdata["expiries"].sort(key=lambda x: x.get("expiry") or "9999-12-31")
        for expiry in pdata.get("expiries", []):
            forward = expiry.get("forward")
            for row in expiry.get("rows", []):
                for side in ("call", "put"):
                    total_quotes += 1
                    quote = row[side]
                    official = eod.get(normalize_symbol(quote.get("symbol", "")))
                    if official is None:
                        continue
                    matched += 1
                    enrich_quote(quote, official, forward)
            recompute_metrics(expiry)

    coverage = matched / total_quotes if total_quotes else 0.0
    result["date"] = run_date.isoformat()
    result["data_fresh"] = True
    result["official_eod_source"] = "CFFEX monthly historical ZIP"
    result["source_status"] = {
        "option_chain": "ok",
        "volume": "ok" if matched > 0 else "missing",
        "official_eod": status,
        "official_quote_matches": matched,
        "total_chain_quotes": total_quotes,
        "official_quote_match_coverage": coverage,
        "freshness": "fresh",
        "freshness_method": "CFFEX official daily CSV existence and HO/IO/MO option rows",
    }

    previous = load_previous(run_date)
    add_changes(result, previous)

    snapshot_path = SNAPSHOT_DIR / f"{run_date.isoformat()}.json"
    snapshot_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    LATEST_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    RADAR_LATEST_PATH.write_text(
        json.dumps(build_radar_summary(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    STATUS_PATH.write_text(
        json.dumps(
            {
                "run_date": run_date.isoformat(),
                "generated_at": now.isoformat(),
                "data_fresh": True,
                "cffex_eod": status,
                "official_quote_matches": matched,
                "total_chain_quotes": total_quotes,
                "official_quote_match_coverage": coverage,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "data_fresh": True,
                "cffex_eod_records": len(eod),
                "official_quote_matches": matched,
                "total_chain_quotes": total_quotes,
                "official_quote_match_coverage": coverage,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
