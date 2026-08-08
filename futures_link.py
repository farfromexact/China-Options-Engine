from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

from eod_enrich import (
    LATEST_PATH,
    RADAR_LATEST_PATH,
    SNAPSHOT_DIR,
    STATUS_PATH,
    TZ_CN,
    clean_text,
    decode_bytes,
    download_daily_csv,
    parse_num,
    pick,
)

FUTURE_PRODUCTS = ("IH", "IF", "IC", "IM")
DIRECT_OPTION_MAP: dict[str, str | None] = {
    "IH": "HO",
    "IF": "IO",
    "IC": None,
    "IM": "MO",
}


def third_friday(year: int, month: int) -> date:
    day = date(year, month, 15)
    while day.weekday() != 4:
        day += timedelta(days=1)
    return day


def parse_future_symbol(raw_symbol: Any) -> tuple[str, str, date] | None:
    symbol = re.sub(r"[^A-Za-z0-9]", "", str(raw_symbol)).upper()
    match = re.fullmatch(r"(IH|IF|IC|IM)(\d{4})", symbol)
    if not match:
        return None
    product, yymm = match.groups()
    expiry = third_friday(2000 + int(yymm[:2]), int(yymm[2:]))
    return product, symbol, expiry


def parse_future_rows_content(
    trade_date: date, source: str, content: bytes
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    text = decode_bytes(content)
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("CFFEX daily CSV has no header")

    products: dict[str, list[dict[str, Any]]] = {product: [] for product in FUTURE_PRODUCTS}
    for row in reader:
        raw_symbol = clean_text(pick(row, "合约代码", "合约", "instrumentId"))
        parsed = parse_future_symbol(raw_symbol)
        if not parsed:
            continue
        product, symbol, expiry = parsed

        close = parse_num(pick(row, "今收盘", "收盘价", "收盘"))
        pre_settle = parse_num(pick(row, "前结算", "前结算价", "昨结算"))
        change_points = close - pre_settle if close is not None and pre_settle not in (None, 0) else None
        change_pct = change_points / pre_settle if change_points is not None and pre_settle else None
        open_interest = parse_num(pick(row, "持仓量", "空盘量"))
        oi_change = parse_num(pick(row, "持仓变化", "持仓量变化", "增减量"))
        previous_oi = open_interest - oi_change if open_interest is not None and oi_change is not None else None
        oi_change_pct = oi_change / previous_oi if oi_change is not None and previous_oi and previous_oi > 0 else None

        products[product].append(
            {
                "symbol": symbol,
                "contract_month": symbol[-4:],
                "expiry_inferred": expiry.isoformat(),
                "expiry_calendar_note": "third Friday inferred; holiday adjustment not applied",
                "dte_calendar": (expiry - trade_date).days,
                "open": parse_num(pick(row, "今开盘", "开盘价", "开盘")),
                "high": parse_num(pick(row, "最高价", "最高")),
                "low": parse_num(pick(row, "最低价", "最低")),
                "close": close,
                "settle": parse_num(pick(row, "今结算", "结算价", "结算")),
                "pre_settle": pre_settle,
                "change_points": change_points,
                "change_pct": change_pct,
                "volume": parse_num(pick(row, "成交量", "成交量(手)", "成交量（手）")),
                "turnover": parse_num(pick(row, "成交金额", "成交额", "成交额(万元)")),
                "open_interest": open_interest,
                "oi_change": oi_change,
                "oi_change_pct": oi_change_pct,
            }
        )

    for contracts in products.values():
        contracts.sort(key=lambda item: (item.get("expiry_inferred") or "9999-12-31", item["symbol"]))

    records = sum(len(items) for items in products.values())
    if records == 0:
        raise RuntimeError("CFFEX daily CSV parsed but no IH/IF/IC/IM futures rows found")

    return products, {
        "status": "ok",
        "trade_date": trade_date.strftime("%Y%m%d"),
        "source": source,
        "records": records,
        "header": reader.fieldnames,
    }


def parse_future_rows(trade_date: date) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    source, content = download_daily_csv(trade_date)
    return parse_future_rows_content(trade_date, source, content)


def summarize_product(product: str, contracts: list[dict[str, Any]]) -> dict[str, Any]:
    active = [
        item
        for item in contracts
        if (item.get("volume") or 0) > 0 or (item.get("open_interest") or 0) > 0
    ]
    if not active:
        return {"product": product, "contracts": contracts, "status": "empty"}

    main = max(active, key=lambda item: ((item.get("volume") or 0), (item.get("open_interest") or 0)))
    front = min(active, key=lambda item: item.get("expiry_inferred") or "9999-12-31")
    remaining = [item for item in active if item["symbol"] != main["symbol"]]
    second_by_volume = (
        max(remaining, key=lambda item: ((item.get("volume") or 0), (item.get("open_interest") or 0)))
        if remaining
        else None
    )
    later = [
        item
        for item in active
        if (item.get("expiry_inferred") or "") > (main.get("expiry_inferred") or "")
    ]
    next_contract = min(later, key=lambda item: item.get("expiry_inferred") or "9999-12-31") if later else second_by_volume

    next_minus_main = None
    annualized_roll_pct = None
    if next_contract and main.get("close") not in (None, 0) and next_contract.get("close") is not None:
        next_minus_main = next_contract["close"] - main["close"]
        day_gap = (date.fromisoformat(next_contract["expiry_inferred"]) - date.fromisoformat(main["expiry_inferred"])).days
        if day_gap > 0:
            annualized_roll_pct = (next_contract["close"] / main["close"] - 1.0) * 365.0 / day_gap

    total_volume = sum(item.get("volume") or 0 for item in active)
    total_oi = sum(item.get("open_interest") or 0 for item in active)
    total_oi_change = sum(item.get("oi_change") or 0 for item in active)

    def slim(item: dict[str, Any] | None) -> dict[str, Any] | None:
        if item is None:
            return None
        keys = (
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
        return {key: item.get(key) for key in keys}

    return {
        "product": product,
        "status": "ok",
        "front_contract": slim(front),
        "main_contract": slim(main),
        "next_contract": slim(next_contract),
        "second_by_volume": slim(second_by_volume),
        "next_minus_main_points": next_minus_main,
        "annualized_roll_pct_inferred": annualized_roll_pct,
        "term_structure_signal": (
            "next_above_main"
            if next_minus_main is not None and next_minus_main > 0
            else "next_below_main"
            if next_minus_main is not None and next_minus_main < 0
            else "flat_or_unavailable"
        ),
        "total_volume": total_volume,
        "total_open_interest": total_oi,
        "total_oi_change": total_oi_change,
        "main_volume_share": (main.get("volume") or 0) / total_volume if total_volume > 0 else None,
        "main_oi_share": (main.get("open_interest") or 0) / total_oi if total_oi > 0 else None,
        "contracts": [slim(item) for item in active[:6]],
    }


def option_expiry_by_symbol(radar: dict[str, Any], option_product: str, target_yymm: str) -> dict[str, Any] | None:
    expiries = radar.get("products", {}).get(option_product, {}).get("expiries", [])
    exact = next((item for item in expiries if str(item.get("symbol", "")).upper() == f"{option_product}{target_yymm}"), None)
    if exact:
        return exact
    if not expiries:
        return None

    def month_number(item: dict[str, Any]) -> int:
        symbol = str(item.get("symbol", ""))
        match = re.search(r"(\d{4})$", symbol)
        if not match:
            return 10**9
        yymm = match.group(1)
        return (2000 + int(yymm[:2])) * 12 + int(yymm[2:])

    target = (2000 + int(target_yymm[:2])) * 12 + int(target_yymm[2:])
    return min(expiries, key=lambda item: abs(month_number(item) - target))


def build_linkage(futures_summary: dict[str, Any], radar: dict[str, Any]) -> dict[str, Any]:
    linkage: dict[str, Any] = {}
    option_metric_keys = (
        "atm_iv",
        "call25_iv",
        "put25_iv",
        "call10_iv",
        "put10_iv",
        "rr25",
        "bf25",
        "pcr_oi",
        "pcr_volume",
        "call_oi",
        "put_oi",
        "call_oi_change",
        "put_oi_change",
        "call_volume",
        "put_volume",
        "gamma_peaks",
        "atm_iv_change_1d",
        "rr25_change_1d",
        "pcr_oi_change_1d",
        "pcr_volume_change_1d",
        "gamma_peak_change_1d",
    )

    for future_product in FUTURE_PRODUCTS:
        summary = futures_summary.get(future_product, {})
        main = summary.get("main_contract") or {}
        target_yymm = str(main.get("contract_month") or "")
        option_product = DIRECT_OPTION_MAP[future_product]

        item: dict[str, Any] = {
            "future_product": future_product,
            "main_future": main or None,
            "direct_option_product": option_product,
            "direct_option_available": option_product is not None,
        }

        if option_product and len(target_yymm) == 4:
            option_expiry = option_expiry_by_symbol(radar, option_product, target_yymm)
            if option_expiry:
                metrics = option_expiry.get("metrics", {})
                option_forward = option_expiry.get("forward")
                future_close = main.get("close")
                difference = (
                    future_close - option_forward
                    if future_close is not None and option_forward not in (None, 0)
                    else None
                )
                item.update(
                    {
                        "matched_option_symbol": option_expiry.get("symbol"),
                        "matched_option_expiry": option_expiry.get("expiry"),
                        "option_forward": option_forward,
                        "future_minus_option_forward_points": difference,
                        "future_minus_option_forward_pct": difference / option_forward if difference is not None and option_forward else None,
                        "option_metrics": {
                            key: metrics.get(key)
                            for key in option_metric_keys
                            if key in metrics
                        },
                    }
                )
            else:
                item["linkage_status"] = "direct option product exists but no matching/near expiry snapshot found"
        else:
            item.update(
                {
                    "linkage_status": "no direct CFFEX CSI 500 index option",
                    "proxy_option_products": ["MO", "IO"],
                    "proxy_warning": "MO/IO and any CSI 500 ETF options are cross-sectional volatility proxies, not one-to-one IC hedges",
                }
            )

        linkage[future_product] = item

    return linkage


def update_status(extra: dict[str, Any]) -> None:
    status: dict[str, Any] = {}
    if STATUS_PATH.exists():
        try:
            status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            status = {}
    status.update(extra)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    now = datetime.now(TZ_CN)
    run_date = now.date()
    if not LATEST_PATH.exists() or not RADAR_LATEST_PATH.exists():
        raise FileNotFoundError("latest.json or radar_latest.json missing before futures linkage")

    latest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    radar = json.loads(RADAR_LATEST_PATH.read_text(encoding="utf-8"))

    if not latest.get("data_fresh") or latest.get("date") != run_date.isoformat():
        update_status(
            {
                "futures_linkage": {
                    "status": "skipped",
                    "reason": "latest option snapshot is not a fresh run-date snapshot",
                    "run_date": run_date.isoformat(),
                }
            }
        )
        print(json.dumps({"futures_linkage": "skipped", "reason": "option snapshot not fresh"}, ensure_ascii=False))
        return

    try:
        raw_products, source_status = parse_future_rows(run_date)
        futures_summary = {
            product: summarize_product(product, raw_products.get(product, []))
            for product in FUTURE_PRODUCTS
        }
        linkage = build_linkage(futures_summary, radar)
        history_linkage = None
        history_products = latest.get("history_products")
        if isinstance(history_products, dict) and history_products:
            history_source = {**latest, "products": history_products}
            history_linkage = build_linkage(
                futures_summary, build_radar_summary(history_source)
            )

        futures_block = {
            "trade_date": run_date.isoformat(),
            "source_status": source_status,
            "products": futures_summary,
            "cross_section": {
                "return_rank": sorted(
                    [
                        {
                            "product": product,
                            "main_symbol": (summary.get("main_contract") or {}).get("symbol"),
                            "change_pct": (summary.get("main_contract") or {}).get("change_pct"),
                        }
                        for product, summary in futures_summary.items()
                        if (summary.get("main_contract") or {}).get("change_pct") is not None
                    ],
                    key=lambda item: item["change_pct"],
                    reverse=True,
                ),
                "oi_change_rank": sorted(
                    [
                        {
                            "product": product,
                            "main_symbol": (summary.get("main_contract") or {}).get("symbol"),
                            "oi_change": (summary.get("main_contract") or {}).get("oi_change"),
                            "oi_change_pct": (summary.get("main_contract") or {}).get("oi_change_pct"),
                        }
                        for product, summary in futures_summary.items()
                        if (summary.get("main_contract") or {}).get("oi_change") is not None
                    ],
                    key=lambda item: item["oi_change_pct"] if item["oi_change_pct"] is not None else float("-inf"),
                    reverse=True,
                ),
            },
        }

        latest["futures"] = futures_block
        latest["futures_option_linkage"] = linkage
        if history_linkage is not None:
            latest["history_futures_option_linkage"] = history_linkage
        radar["futures"] = futures_block
        radar["futures_option_linkage"] = linkage
        latest.setdefault("source_status", {})["futures"] = source_status
        radar.setdefault("source_status", {})["futures"] = source_status

        LATEST_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
        RADAR_LATEST_PATH.write_text(json.dumps(radar, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot_path = SNAPSHOT_DIR / f"{run_date.isoformat()}.json"
        snapshot_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

        update_status(
            {
                "futures_linkage": {
                    "status": "ok",
                    "trade_date": run_date.isoformat(),
                    "records": source_status.get("records"),
                    "products": list(FUTURE_PRODUCTS),
                    "direct_pairs": {"IH": "HO", "IF": "IO", "IM": "MO"},
                    "IC_note": "no direct CFFEX CSI 500 index option; proxy only",
                }
            }
        )
        print(
            json.dumps(
                {
                    "futures_linkage": "ok",
                    "records": source_status.get("records"),
                    "main_contracts": {
                        product: (summary.get("main_contract") or {}).get("symbol")
                        for product, summary in futures_summary.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except Exception as exc:
        update_status(
            {
                "futures_linkage": {
                    "status": "missing",
                    "run_date": run_date.isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            }
        )
        print(json.dumps({"futures_linkage": "missing", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    main()
