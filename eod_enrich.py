from __future__ import annotations

import csv
import json
import math
import os
import re
import time
import zipfile
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Mapping

import requests

TZ_CN = timezone(timedelta(hours=8))
REQ_TIMEOUT = 25
REFERER = "http://www.cffex.com.cn/rtj/"
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

DATA_DIR = Path("data")
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LATEST_PATH = DATA_DIR / "latest.json"
RADAR_LATEST_PATH = DATA_DIR / "radar_latest.json"
STATUS_PATH = DATA_DIR / "last_run_status.json"

OPTION_PRODUCTS = ("HO", "IO", "MO")
OPTION_PREFIXES = set(OPTION_PRODUCTS)
MIN_OFFICIAL_CHAIN_COVERAGE = 0.95
OPTION_CONTRACT_PATTERN = re.compile(
    r"^(HO|IO|MO)(\d{4})([CP])(\d+(?:\.\d+)?)$", re.IGNORECASE
)
HISTORY_PRICE_FIELD = "history_price_eod"
HISTORY_OPTION_PRICE_BASIS = "cffex_official_settlement_fallback_close"


def parse_num(value: Any) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return None
        if isinstance(value, str):
            value = value.replace(",", "").replace("％", "%").strip()
            if value.endswith("%"):
                value = value[:-1]
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text.lower() not in {"nan", "none", "null"} else None


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(symbol)).upper()


def norm_key(key: Any) -> str:
    return re.sub(r"\s+", "", str(key)).lower()


def pick(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    normalized = {norm_key(k): v for k, v in row.items()}
    for key in keys:
        value = normalized.get(norm_key(key))
        if value not in (None, ""):
            return value
    return None


def decode_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "gb2312"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("gb18030", errors="ignore")


def create_session() -> requests.Session:
    session = requests.Session()
    # GitHub-hosted jobs should not inherit an accidental local proxy, while
    # users who intentionally rely on HTTP(S)_PROXY can opt back in.
    session.trust_env = os.getenv("CFFEX_TRUST_ENV", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    session.headers.update(BROWSER_HEADERS)
    return session


def fetch_first_available(
    session: requests.Session,
    urls: list[str],
    *,
    min_size: int = 50,
    attempts: int = 3,
) -> tuple[str, bytes]:
    last_error: str | None = None
    for url in urls:
        for attempt in range(1, attempts + 1):
            try:
                response = session.get(
                    url,
                    headers={**BROWSER_HEADERS, "Referer": REFERER},
                    timeout=REQ_TIMEOUT,
                )
                response.raise_for_status()
                content = response.content or b""
                if len(content) >= min_size:
                    return url, content
                last_error = f"{url}: response too small ({len(content)} bytes)"
            except Exception as exc:
                last_error = f"{url}: {type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(0.5 * attempt)
        time.sleep(0.3)
    raise RuntimeError(last_error or "all CFFEX endpoints failed")


def download_single_daily_csv(trade_date: date) -> tuple[str, bytes]:
    ds = trade_date.strftime("%Y%m%d")
    yyyymm = ds[:6]
    dd = ds[6:]
    single = [
        f"http://www.cffex.com.cn/sj/hqsj/rtj/{yyyymm}/{dd}/{ds}_1.csv",
        f"http://www.cffex.com.cn/fzjy/mrhq/{yyyymm}/{dd}/{ds}_1.csv",
        f"https://www.cffex.com.cn/sj/hqsj/rtj/{yyyymm}/{dd}/{ds}_1.csv",
        f"https://www.cffex.com.cn/fzjy/mrhq/{yyyymm}/{dd}/{ds}_1.csv",
    ]
    return fetch_first_available(create_session(), single, min_size=100)


def download_monthly_zip(yyyymm: str) -> tuple[str, bytes]:
    if not re.fullmatch(r"\d{6}", yyyymm):
        raise ValueError("yyyymm must use YYYYMM format")
    monthly_urls = [
        f"http://www.cffex.com.cn/sj/historysj/{yyyymm}/zip/{yyyymm}.zip",
        f"https://www.cffex.com.cn/sj/historysj/{yyyymm}/zip/{yyyymm}.zip",
    ]
    # The backfill provider probes each month once and then falls back to daily
    # files, so avoid multiplying a slow missing-month response by retries.
    return fetch_first_available(create_session(), monthly_urls, min_size=100, attempts=1)


def extract_daily_from_monthly_zip(
    source: str, content: bytes, trade_date: date
) -> tuple[str, bytes]:
    ds = trade_date.strftime("%Y%m%d")
    target = f"{ds}_1.csv"
    with zipfile.ZipFile(BytesIO(content)) as archive:
        match = next(
            (
                name
                for name in archive.namelist()
                if name == target
                or name.replace("\\", "/").endswith(f"/{target}")
            ),
            None,
        )
        if match is None:
            raise FileNotFoundError(f"{target} not found in monthly ZIP")
        return f"{source}#{match}", archive.read(match)


def download_daily_csv(trade_date: date) -> tuple[str, bytes]:
    yyyymm = trade_date.strftime("%Y%m")

    try:
        return download_single_daily_csv(trade_date)
    except Exception as single_error:
        try:
            source, content = download_monthly_zip(yyyymm)
            return extract_daily_from_monthly_zip(source, content, trade_date)
        except Exception as zip_error:
            raise RuntimeError(
                f"single CSV failed: {single_error}; monthly ZIP failed: {zip_error}"
            ) from zip_error


def parse_cffex_eod_content(
    trade_date: date, source: str, content: bytes
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    ds = trade_date.strftime("%Y%m%d")
    text = decode_bytes(content)
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("CFFEX daily CSV has no header")

    records: dict[str, dict[str, Any]] = {}
    samples: list[str] = []

    for row in reader:
        raw_symbol = clean_text(pick(row, "合约代码", "合约", "instrumentId"))
        if not raw_symbol or any(word in raw_symbol for word in ("小计", "合计", "总计")):
            continue

        normalized = normalize_symbol(raw_symbol)
        variety_match = re.match(r"^([A-Z]+)", normalized)
        variety = variety_match.group(1) if variety_match else None
        if variety not in OPTION_PREFIXES:
            continue

        if len(samples) < 10:
            samples.append(raw_symbol)

        records[normalized] = {
            "raw_symbol": raw_symbol,
            "volume": parse_num(pick(row, "成交量", "成交量(手)", "成交量（手）")),
            "open_interest": parse_num(pick(row, "持仓量", "空盘量")),
            "oi_change": parse_num(pick(row, "持仓变化", "持仓量变化", "增减量")),
            "close": parse_num(pick(row, "今收盘", "收盘价", "收盘")),
            "settle": parse_num(pick(row, "今结算", "结算价", "结算")),
            "pre_settle": parse_num(pick(row, "前结算", "前结算价", "昨结算")),
            "delta_official": parse_num(pick(row, "Delta", "DELTA", "delta")),
            "iv_official": parse_num(pick(row, "隐含波动率", "IV", "impliedVolatility")),
        }

    if not records:
        raise RuntimeError(
            f"CFFEX daily CSV parsed but no HO/IO/MO rows found; samples={samples}"
        )

    return records, {
        "status": "ok",
        "trade_date": ds,
        "source": source,
        "records": len(records),
        "header": reader.fieldnames,
        "sample_symbols": samples,
    }


def fetch_cffex_eod(trade_date: date) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    ds = trade_date.strftime("%Y%m%d")
    try:
        source, content = download_daily_csv(trade_date)
        return parse_cffex_eod_content(trade_date, source, content)
    except Exception as exc:
        return {}, {
            "status": "missing",
            "trade_date": ds,
            "error": f"{type(exc).__name__}: {exc}",
        }


def enrich_quote(quote: dict[str, Any], official: dict[str, Any], forward: float | None) -> None:
    official_oi = official.get("open_interest")
    quote["volume"] = official.get("volume")
    quote["oi_change"] = official.get("oi_change")
    quote["official_oi"] = official_oi
    quote["close_eod"] = official.get("close")
    quote["settle_eod"] = official.get("settle")
    quote["pre_settle_eod"] = official.get("pre_settle")
    quote["delta_official"] = official.get("delta_official")
    quote["iv_official"] = official.get("iv_official")

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
            "volume_scope": "CFFEX official daily EOD, full listed option chain for this expiry",
        }
    )


def parse_option_contract(symbol: str) -> tuple[str, str, str, float] | None:
    match = OPTION_CONTRACT_PATTERN.fullmatch(str(symbol).upper())
    if not match:
        return None
    product, yymm, cp, strike = match.groups()
    return product, yymm, cp, float(strike)


def _official_history_quote(symbol: str, official: Mapping[str, Any]) -> dict[str, Any]:
    settle = parse_num(official.get("settle"))
    close = parse_num(official.get("close"))
    model_price = settle if settle is not None and settle > 0 else close
    if model_price is None or model_price <= 0:
        raise RuntimeError(f"official option {symbol} has no positive settlement or close")
    return {
        "symbol": symbol,
        "last": close,
        "oi": parse_num(official.get("open_interest")),
        "volume": parse_num(official.get("volume")),
        "oi_change": parse_num(official.get("oi_change")),
        "official_oi": parse_num(official.get("open_interest")),
        "close_eod": close,
        "settle_eod": settle,
        "pre_settle_eod": parse_num(official.get("pre_settle")),
        "delta_official": parse_num(official.get("delta_official")),
        "iv_official": parse_num(official.get("iv_official")),
        HISTORY_PRICE_FIELD: model_price,
    }


def build_official_settlement_products(
    official_records: Mapping[str, Mapping[str, Any]], market_date: date
) -> dict[str, Any]:
    from engine import add_analytics, expiry_from_symbol

    grouped: dict[tuple[str, str, float], dict[str, dict[str, Any]]] = {}
    active_counts = {product: 0 for product in OPTION_PRODUCTS}
    for raw_symbol, official in official_records.items():
        symbol = normalize_symbol(raw_symbol)
        parsed = parse_option_contract(symbol)
        if parsed is None:
            continue
        product, yymm, cp, strike = parsed
        expiry = expiry_from_symbol(f"{product}{yymm}")
        if expiry <= market_date:
            continue
        active_counts[product] += 1
        pair = grouped.setdefault((product, yymm, strike), {})
        side = "call" if cp == "C" else "put"
        if side in pair:
            raise RuntimeError(f"duplicate official option contract: {symbol}")
        pair[side] = _official_history_quote(symbol, official)

    missing_products = [product for product, count in active_counts.items() if count == 0]
    if missing_products:
        raise RuntimeError(f"official option data missing active products: {missing_products}")

    expiry_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (product, yymm, strike), pair in grouped.items():
        if set(pair) != {"call", "put"}:
            raise RuntimeError(
                f"official option strike is not call/put paired: {product}{yymm} {strike}"
            )
        expiry_rows.setdefault((product, yymm), []).append(
            {"strike": strike, "call": pair["call"], "put": pair["put"]}
        )

    calculation_time = datetime.combine(
        market_date, datetime.min.time(), tzinfo=TZ_CN
    ).replace(hour=15)
    products: dict[str, Any] = {product: {"expiries": []} for product in OPTION_PRODUCTS}
    for (product, yymm), rows in sorted(expiry_rows.items()):
        rows.sort(key=lambda item: item["strike"])
        expiry = expiry_from_symbol(f"{product}{yymm}")
        analyzed = add_analytics(
            rows,
            expiry,
            calculation_time,
            price_field=HISTORY_PRICE_FIELD,
        )
        analyzed["symbol"] = f"{product}{yymm}"
        analyzed.setdefault("metrics", {})[
            "pricing_basis"
        ] = HISTORY_OPTION_PRICE_BASIS
        recompute_metrics(analyzed)
        products[product]["expiries"].append(analyzed)

    for product, payload in products.items():
        expiries = payload["expiries"]
        expiries.sort(key=lambda item: item.get("expiry") or "9999-12-31")
        if not expiries:
            raise RuntimeError(f"official option data produced no active {product} expiries")
        for expiry in expiries[:4]:
            metrics = expiry.get("metrics", {})
            if (
                expiry.get("forward") is None
                or metrics.get("atm_iv") is None
                or metrics.get("pcr_oi") is None
                or not metrics.get("gamma_peaks")
            ):
                raise RuntimeError(
                    f"official settlement analytics incomplete for {expiry.get('symbol')}"
                )
    return products


def build_official_settlement_history_products(
    official_records: Mapping[str, Mapping[str, Any]], market_date: date
) -> dict[str, Any]:
    return compact_history_products(
        build_official_settlement_products(official_records, market_date)
    )


def compact_history_products(products: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for product, payload in products.items():
        expiries = []
        for expiry in payload.get("expiries", []):
            expiries.append(
                {
                    "symbol": expiry.get("symbol"),
                    "expiry": expiry.get("expiry"),
                    "forward": expiry.get("forward"),
                    "metrics": deepcopy(expiry.get("metrics", {})),
                }
            )
        compact[str(product)] = {"expiries": expiries}
    return compact


def build_settlement_history_products(
    products: Mapping[str, Any], market_date: date
) -> dict[str, Any]:
    from engine import add_analytics

    calculation_time = datetime.combine(
        market_date, datetime.min.time(), tzinfo=TZ_CN
    ).replace(hour=15)
    repriced: dict[str, Any] = {}
    for product, payload in products.items():
        expiries: list[dict[str, Any]] = []
        for source_expiry in payload.get("expiries", []):
            expiry_value = source_expiry.get("expiry")
            if not isinstance(expiry_value, str):
                continue
            expiry_date = date.fromisoformat(expiry_value)
            if expiry_date <= market_date:
                continue
            rows = deepcopy(source_expiry.get("rows", []))
            for row in rows:
                for side in ("call", "put"):
                    quote = row[side]
                    settle = parse_num(quote.get("settle_eod"))
                    close = parse_num(quote.get("close_eod"))
                    quote[HISTORY_PRICE_FIELD] = (
                        settle if settle is not None and settle > 0 else close
                    )
            analyzed = add_analytics(
                rows,
                expiry_date,
                calculation_time,
                price_field=HISTORY_PRICE_FIELD,
            )
            analyzed["symbol"] = source_expiry.get("symbol")
            analyzed.setdefault("metrics", {})[
                "pricing_basis"
            ] = HISTORY_OPTION_PRICE_BASIS
            recompute_metrics(analyzed)
            expiries.append(analyzed)

        expiries.sort(key=lambda item: item.get("expiry") or "9999-12-31")
        for expiry in expiries[:4]:
            metrics = expiry.get("metrics", {})
            if (
                expiry.get("forward") is None
                or metrics.get("atm_iv") is None
                or metrics.get("pcr_oi") is None
                or not metrics.get("gamma_peaks")
            ):
                raise RuntimeError(
                    f"official settlement analytics incomplete for {expiry.get('symbol')}"
                )
        repriced[str(product)] = {"expiries": expiries}
    if set(repriced) != OPTION_PREFIXES:
        raise RuntimeError("settlement history requires HO, IO, and MO option products")
    empty_products = [
        product for product in OPTION_PRODUCTS if not repriced[product].get("expiries")
    ]
    if empty_products:
        raise RuntimeError(
            f"settlement history has no active expiries for products: {empty_products}"
        )
    return compact_history_products(repriced)


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
    metric_keys = [
        "atm_iv", "call25_iv", "put25_iv", "call10_iv", "put10_iv",
        "rr25", "bf25", "call_oi", "put_oi", "pcr_oi",
        "call_volume", "put_volume", "pcr_volume",
        "call_oi_change", "put_oi_change", "volume_oi_coverage", "gamma_peaks",
        "atm_iv_change_1d", "rr25_change_1d", "pcr_oi_change_1d",
        "call_oi_change_1d", "put_oi_change_1d",
        "pcr_volume_change_1d", "call_volume_change_1d", "put_volume_change_1d",
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
            key=lambda item: item.get("expiry") or "9999-12-31",
        )
        compact["products"][product] = {
            "expiries": [
                {
                    "symbol": expiry.get("symbol"),
                    "expiry": expiry.get("expiry"),
                    "forward": expiry.get("forward"),
                    "metrics": {
                        key: expiry.get("metrics", {}).get(key)
                        for key in metric_keys
                        if key in expiry.get("metrics", {})
                    },
                }
                for expiry in ordered[:4]
            ]
        }

    # Dated snapshots receive futures linkage after the EOD option pass. Preserve
    # those blocks when a holiday/source failure restores the latest verified day.
    if "futures" in result:
        compact["futures"] = result["futures"]
    if "futures_option_linkage" in result:
        compact["futures_option_linkage"] = result["futures_option_linkage"]

    return compact


def is_verified_snapshot(snapshot: Any, snapshot_date: str) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("data_fresh") is not True or snapshot.get("date") != snapshot_date:
        return False
    source_status = snapshot.get("source_status")
    if not isinstance(source_status, dict) or source_status.get("freshness") != "fresh":
        return False
    if source_status.get("option_chain") != "ok" or source_status.get("volume") != "ok":
        return False
    coverage = source_status.get("official_quote_match_coverage")
    if (
        not isinstance(coverage, (int, float))
        or float(coverage) < MIN_OFFICIAL_CHAIN_COVERAGE
    ):
        return False
    official = source_status.get("official_eod")
    if not isinstance(official, dict) or official.get("status") != "ok":
        return False
    official_date = str(official.get("trade_date") or "").replace("-", "")
    if official_date != snapshot_date.replace("-", ""):
        return False
    futures = snapshot.get("futures")
    if not isinstance(futures, dict):
        return False
    futures_status = futures.get("source_status")
    if not isinstance(futures_status, dict) or futures_status.get("status") != "ok":
        return False
    futures_date = str(futures_status.get("trade_date") or "").replace("-", "")
    if futures_date != snapshot_date.replace("-", ""):
        return False
    futures_products = futures.get("products")
    if not isinstance(futures_products, dict):
        return False
    for product in ("IH", "IF", "IC", "IM"):
        summary = futures_products.get(product)
        if not isinstance(summary, dict) or summary.get("status") != "ok":
            return False
        main_contract = summary.get("main_contract")
        if not isinstance(main_contract, dict) or not main_contract.get("symbol"):
            return False

    option_products = snapshot.get("history_products") or snapshot.get("products")
    if not isinstance(option_products, dict):
        return False
    for product in OPTION_PRODUCTS:
        payload = option_products.get(product)
        if not isinstance(payload, dict):
            return False
        expiries = payload.get("expiries")
        if not isinstance(expiries, list) or not expiries:
            return False
    return True


def restore_latest_verified() -> dict[str, Any] | None:
    snapshots = sorted(SNAPSHOT_DIR.glob("*.json"), reverse=True)
    for snapshot in snapshots:
        try:
            verified = json.loads(snapshot.read_text(encoding="utf-8"))
            if not is_verified_snapshot(verified, snapshot.stem):
                continue
            LATEST_PATH.write_text(
                json.dumps(verified, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            RADAR_LATEST_PATH.write_text(
                json.dumps(build_radar_summary(verified), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return verified
        except Exception:
            continue
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
    live_symbols: set[str] = set()

    for pdata in result.get("products", {}).values():
        pdata["expiries"].sort(key=lambda item: item.get("expiry") or "9999-12-31")
        for expiry in pdata.get("expiries", []):
            forward = expiry.get("forward")
            for row in expiry.get("rows", []):
                for side in ("call", "put"):
                    total_quotes += 1
                    quote = row[side]
                    normalized_symbol = normalize_symbol(quote.get("symbol", ""))
                    if normalized_symbol:
                        live_symbols.add(normalized_symbol)
                    official = eod.get(normalized_symbol)
                    if official is None:
                        continue
                    matched += 1
                    enrich_quote(quote, official, forward)
            recompute_metrics(expiry)

    from engine import expiry_from_symbol

    active_official_symbols: set[str] = set()
    for symbol in eod:
        parsed = parse_option_contract(symbol)
        if parsed is None:
            continue
        product, yymm, _, _ = parsed
        if expiry_from_symbol(f"{product}{yymm}") > run_date:
            active_official_symbols.add(symbol)
    matched_active_symbols = active_official_symbols & live_symbols
    coverage = (
        len(matched_active_symbols) / len(active_official_symbols)
        if active_official_symbols
        else 0.0
    )
    missing_active_symbols = active_official_symbols - live_symbols
    if coverage < MIN_OFFICIAL_CHAIN_COVERAGE:
        sample = sorted(missing_active_symbols)[:10]
        raise RuntimeError(
            "live option chain coverage is below the minimum CFFEX EOD threshold: "
            f"coverage={coverage:.6f}, minimum={MIN_OFFICIAL_CHAIN_COVERAGE:.2f}, "
            f"missing={len(missing_active_symbols)}, sample={sample}"
        )
    history_products = build_official_settlement_history_products(eod, run_date)

    result["date"] = run_date.isoformat()
    result["data_fresh"] = True
    result["history_products"] = history_products
    result["history_record_origin"] = "scheduled_eod"
    result["history_option_price_basis"] = HISTORY_OPTION_PRICE_BASIS
    result["official_eod_source"] = status.get("source")
    result["source_status"] = {
        "option_chain": "ok",
        "volume": "ok",
        "official_eod": status,
        "official_quote_matches": len(matched_active_symbols),
        "total_chain_quotes": len(active_official_symbols),
        "official_quote_match_coverage": coverage,
        "official_unmatched_active_symbols": len(missing_active_symbols),
        "official_unmatched_active_sample": sorted(missing_active_symbols)[:10],
        "sina_chain_quotes": total_quotes,
        "all_official_matches_including_expired": matched,
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
                "official_unmatched_active_symbols": len(missing_active_symbols),
                "official_unmatched_active_sample": sorted(missing_active_symbols)[:10],
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
                "official_unmatched_active_symbols": len(missing_active_symbols),
                "official_unmatched_active_sample": sorted(missing_active_symbols)[:10],
                "source": status.get("source"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
