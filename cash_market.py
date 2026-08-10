from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import requests


TZ_CN = timezone(timedelta(hours=8))
REQ_TIMEOUT = 15
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# CFFEX product -> cash index / representative on-exchange ETF.
# CSI 500 is included as the natural cash anchor for IC even though the radar's
# mandatory spot set is SSE50 / CSI300 / CSI1000.
CASH_REFERENCE: dict[str, dict[str, str]] = {
    "IH": {
        "index_code": "000016",
        "index_name": "SSE50",
        "index_quote": "sh000016",
        "etf_code": "510050",
        "etf_name": "SSE50 ETF",
        "etf_quote": "sh510050",
        "eastmoney_secid": "1.510050",
    },
    "IF": {
        "index_code": "000300",
        "index_name": "CSI300",
        "index_quote": "sh000300",
        "etf_code": "510300",
        "etf_name": "CSI300 ETF",
        "etf_quote": "sh510300",
        "eastmoney_secid": "1.510300",
    },
    "IC": {
        "index_code": "000905",
        "index_name": "CSI500",
        "index_quote": "sh000905",
        "etf_code": "510500",
        "etf_name": "CSI500 ETF",
        "etf_quote": "sh510500",
        "eastmoney_secid": "1.510500",
    },
    "IM": {
        "index_code": "000852",
        "index_name": "CSI1000",
        "index_quote": "sh000852",
        "etf_code": "512100",
        "etf_name": "CSI1000 ETF",
        "etf_quote": "sh512100",
        "eastmoney_secid": "1.512100",
    },
}

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"


def _num(value: Any) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = os.getenv("PUBLIC_MARKET_TRUST_ENV", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    return session


def parse_tencent_quote_text(text: str) -> dict[str, dict[str, Any]]:
    """Parse Tencent's public quote payload.

    Core quote fields are the stable `~`-delimited public payload. For listed
    ETFs Tencent also publishes exact listed-share counts in the extended
    payload. We retain that raw share count only when the instrument type is
    explicitly ETF, and cross-check it against total market cap / price when
    those fields are present.
    """
    result: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or '="' not in line:
            continue
        lhs, rhs = line.split('="', 1)
        symbol = lhs.rsplit("v_", 1)[-1].strip()
        payload = rhs.rsplit('"', 1)[0]
        values = payload.split("~")
        if len(values) < 35:
            continue

        price = _num(values[3])
        previous_close = _num(values[4])
        change_amount = _num(values[31]) if len(values) > 31 else None
        change_pct_percent = _num(values[32]) if len(values) > 32 else None
        if change_amount is None and price is not None and previous_close is not None:
            change_amount = price - previous_close
        change_pct = (
            change_pct_percent / 100.0
            if change_pct_percent is not None
            else (price / previous_close - 1.0)
            if price is not None and previous_close not in (None, 0)
            else None
        )

        security_type = values[61].strip() if len(values) > 61 and values[61] else None
        float_market_cap_cny = (
            _num(values[44]) * 100_000_000.0
            if len(values) > 44 and _num(values[44]) is not None
            else None
        )
        total_market_cap_cny = (
            _num(values[45]) * 100_000_000.0
            if len(values) > 45 and _num(values[45]) is not None
            else None
        )

        listed_shares = None
        listed_shares_field = None
        if security_type == "ETF":
            # Extended A-share/ETF payloads currently expose listed share
            # counts at 72/73/76; for exchange-listed ETFs these values are
            # expected to agree because the fund units are fully tradable.
            for field_index in (72, 73, 76):
                candidate = _num(values[field_index]) if len(values) > field_index else None
                if candidate is not None and candidate > 0:
                    listed_shares = candidate
                    listed_shares_field = f"tencent_extended_{field_index}"
                    break

        share_cap_crosscheck_pct = None
        if (
            listed_shares is not None
            and price not in (None, 0)
            and total_market_cap_cny not in (None, 0)
        ):
            implied_market_cap = listed_shares * price
            share_cap_crosscheck_pct = implied_market_cap / total_market_cap_cny - 1.0
            # Tencent market cap is rounded to 0.01 yi CNY; a wider mismatch
            # indicates payload-layout drift, so do not consume the share count.
            if abs(share_cap_crosscheck_pct) > 0.002:
                listed_shares = None
                listed_shares_field = None

        result[symbol] = {
            "symbol": symbol,
            "name": values[1] or None,
            "code": values[2] or symbol[-6:],
            "security_type": security_type,
            "close": price,
            "previous_close": previous_close,
            "open": _num(values[5]) if len(values) > 5 else None,
            "high": _num(values[33]) if len(values) > 33 else None,
            "low": _num(values[34]) if len(values) > 34 else None,
            "change_amount": change_amount,
            "change_pct": change_pct,
            "turnover_cny": (
                _num(values[37]) * 10000.0
                if len(values) > 37 and _num(values[37]) is not None
                else None
            ),
            "float_market_cap_cny": float_market_cap_cny,
            "total_market_cap_cny": total_market_cap_cny,
            "listed_shares": listed_shares,
            "listed_shares_field": listed_shares_field,
            "share_cap_crosscheck_pct": share_cap_crosscheck_pct,
            "quote_timestamp_raw": values[30] if len(values) > 30 and values[30] else None,
        }
    return result


def fetch_tencent_quotes(session: requests.Session) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    symbols = []
    for item in CASH_REFERENCE.values():
        symbols.extend([item["index_quote"], item["etf_quote"]])
    symbols = list(dict.fromkeys(symbols))
    url = TENCENT_QUOTE_URL + ",".join(symbols)
    response = session.get(
        url,
        headers={"Referer": "https://gu.qq.com/", "User-Agent": UA},
        timeout=REQ_TIMEOUT,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    quotes = parse_tencent_quote_text(response.text)
    missing = [symbol for symbol in symbols if symbol not in quotes]
    if missing:
        raise RuntimeError(f"Tencent public quote missing symbols: {missing}")
    return quotes, {
        "status": "ok",
        "source": "Tencent public quote endpoint qt.gtimg.cn",
        "records": len(quotes),
        "symbols": symbols,
    }


def parse_eastmoney_share_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise RuntimeError("Eastmoney public quote returned no data object")
    total_shares = _num(data.get("f84"))
    float_shares = _num(data.get("f85"))
    if total_shares is None or total_shares <= 0:
        total_shares = float_shares
        share_field = "f85_float_shares_fallback"
    else:
        share_field = "f84_total_shares"
    if total_shares is None or total_shares <= 0:
        raise RuntimeError("Eastmoney public quote has no positive ETF share count")
    return {
        "code": str(data.get("f57") or ""),
        "name": data.get("f58"),
        "total_shares": total_shares,
        "float_shares": float_shares,
        "share_field": share_field,
        "share_source": "eastmoney_fallback",
        "market_cap_cny": _num(data.get("f116")),
        "float_market_cap_cny": _num(data.get("f117")),
        "quote_epoch": _num(data.get("f124")),
    }


def fetch_etf_shares(
    session: requests.Session, secid: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    params = {
        "secid": secid,
        "fields": "f57,f58,f84,f85,f116,f117,f124",
        "fltt": "2",
        "invt": "2",
    }
    response = session.get(
        EASTMONEY_QUOTE_URL,
        params=params,
        headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
        timeout=REQ_TIMEOUT,
    )
    response.raise_for_status()
    parsed = parse_eastmoney_share_payload(response.json())
    return parsed, {
        "status": "ok",
        "source": "Eastmoney public quote endpoint push2.eastmoney.com",
        "secid": secid,
        "share_field": parsed.get("share_field"),
    }


def _load_previous_cash_market(snapshot_dir: Path, run_date: date) -> dict[str, Any] | None:
    for path in sorted(snapshot_dir.glob("*.json"), reverse=True):
        if path.stem >= run_date.isoformat():
            continue
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        futures = snapshot.get("futures")
        if not isinstance(futures, Mapping):
            continue
        cash_market = futures.get("cash_market")
        if isinstance(cash_market, Mapping):
            return dict(cash_market)
    return None


def _previous_etf_shares(previous: Mapping[str, Any] | None, code: str) -> float | None:
    if not isinstance(previous, Mapping):
        return None
    etfs = previous.get("reference_etfs")
    if not isinstance(etfs, Mapping):
        return None
    payload = etfs.get(code)
    if not isinstance(payload, Mapping):
        return None
    return _num(payload.get("total_shares"))


def _shares_from_tencent_quote(etf_quote: Mapping[str, Any]) -> dict[str, Any] | None:
    listed_shares = _num(etf_quote.get("listed_shares"))
    if listed_shares is None or listed_shares <= 0:
        return None
    return {
        "total_shares": listed_shares,
        "float_shares": listed_shares,
        "share_field": etf_quote.get("listed_shares_field"),
        "share_source": "tencent_extended_quote",
        "market_cap_cny": _num(etf_quote.get("total_market_cap_cny")),
        "float_market_cap_cny": _num(etf_quote.get("float_market_cap_cny")),
        "share_cap_crosscheck_pct": _num(etf_quote.get("share_cap_crosscheck_pct")),
    }


def collect_cash_market(run_date: date, snapshot_dir: Path) -> dict[str, Any]:
    """Collect cash-index and representative ETF data from public endpoints.

    ETF flow is a creation/redemption *estimate*: change in listed ETF total
    shares multiplied by the ETF market close. It is not order flow and is not
    claimed to be exact cash movement because ETF primary-market activity can
    be in-kind.
    """
    session = _session()
    errors: list[str] = []
    previous = _load_previous_cash_market(snapshot_dir, run_date)

    try:
        quotes, quote_status = fetch_tencent_quotes(session)
    except Exception as exc:
        quotes = {}
        quote_status = {
            "status": "missing",
            "error": f"{type(exc).__name__}: {exc}",
        }
        errors.append(f"quotes: {type(exc).__name__}: {exc}")

    indices: dict[str, Any] = {}
    etfs: dict[str, Any] = {}
    share_status: dict[str, Any] = {}

    for future_product, ref in CASH_REFERENCE.items():
        index_quote = quotes.get(ref["index_quote"], {})
        indices[ref["index_code"]] = {
            "future_product": future_product,
            "code": ref["index_code"],
            "name": ref["index_name"],
            **index_quote,
        }

        etf_quote = quotes.get(ref["etf_quote"], {})
        shares = _shares_from_tencent_quote(etf_quote)
        if shares is not None:
            share_status[ref["etf_code"]] = {
                "status": "ok",
                "source": "Tencent extended ETF quote fields",
                "share_field": shares.get("share_field"),
                "market_cap_crosscheck_pct": shares.get("share_cap_crosscheck_pct"),
            }
        else:
            try:
                shares, status = fetch_etf_shares(session, ref["eastmoney_secid"])
                share_status[ref["etf_code"]] = {
                    **status,
                    "fallback_reason": "Tencent extended listed-share field unavailable or failed cross-check",
                }
            except Exception as exc:
                shares = {}
                share_status[ref["etf_code"]] = {
                    "status": "missing",
                    "error": f"{type(exc).__name__}: {exc}",
                    "fallback_reason": "Tencent extended listed-share field unavailable or failed cross-check",
                }
                errors.append(
                    f"ETF {ref['etf_code']} shares: {type(exc).__name__}: {exc}"
                )

        total_shares = _num(shares.get("total_shares"))
        previous_shares = _previous_etf_shares(previous, ref["etf_code"])
        share_change = (
            total_shares - previous_shares
            if total_shares is not None and previous_shares is not None
            else None
        )
        share_change_pct = (
            share_change / previous_shares
            if share_change is not None and previous_shares not in (None, 0)
            else None
        )
        close = _num(etf_quote.get("close"))
        estimated_flow = (
            share_change * close
            if share_change is not None and close is not None
            else None
        )

        etfs[ref["etf_code"]] = {
            "future_product": future_product,
            "code": ref["etf_code"],
            "name": ref["etf_name"],
            **etf_quote,
            **shares,
            "previous_total_shares": previous_shares,
            "share_change": share_change,
            "share_change_pct": share_change_pct,
            "estimated_net_creation_redemption_cny": estimated_flow,
            "flow_method": "daily total-share change * ETF market close",
            "flow_interpretation": (
                "creation/redemption estimate; not secondary-market order flow; "
                "market close is used as a value proxy rather than same-day NAV"
            ),
            "nav": None,
            "nav_status": "not_required_for_flow; market close proxy used",
        }

    index_ok = all(_num(item.get("close")) is not None for item in indices.values())
    share_ok_count = sum(
        1 for item in etfs.values() if _num(item.get("total_shares")) is not None
    )
    overall = "ok" if index_ok and share_ok_count == len(CASH_REFERENCE) else "partial"
    if not quotes and share_ok_count == 0:
        overall = "missing"

    return {
        "trade_date": run_date.isoformat(),
        "generated_at": datetime.now(TZ_CN).isoformat(),
        "status": overall,
        "indices": indices,
        "reference_etfs": etfs,
        "source_status": {
            "quotes": quote_status,
            "etf_shares": share_status,
            "index_close_coverage": sum(
                1 for item in indices.values() if _num(item.get("close")) is not None
            )
            / len(CASH_REFERENCE),
            "etf_share_coverage": share_ok_count / len(CASH_REFERENCE),
        },
        "errors": errors,
    }


def enrich_futures_with_cash_market(
    futures_summary: dict[str, Any], cash_market: Mapping[str, Any]
) -> None:
    indices = cash_market.get("indices")
    etfs = cash_market.get("reference_etfs")
    if not isinstance(indices, Mapping):
        indices = {}
    if not isinstance(etfs, Mapping):
        etfs = {}

    for product, summary in futures_summary.items():
        ref = CASH_REFERENCE.get(product)
        if not ref or not isinstance(summary, dict):
            continue
        index = indices.get(ref["index_code"])
        etf = etfs.get(ref["etf_code"])
        index = index if isinstance(index, Mapping) else {}
        etf = etf if isinstance(etf, Mapping) else {}
        main = summary.get("main_contract")
        main = main if isinstance(main, Mapping) else {}

        spot_close = _num(index.get("close"))
        future_close = _num(main.get("close"))
        basis_points = (
            future_close - spot_close
            if future_close is not None and spot_close is not None
            else None
        )
        basis_pct = (
            basis_points / spot_close
            if basis_points is not None and spot_close not in (None, 0)
            else None
        )
        dte = _num(main.get("dte_calendar"))
        annualized_basis = (
            basis_pct * 365.0 / dte
            if basis_pct is not None and dte is not None and dte > 0
            else None
        )

        summary.update(
            {
                "cash_index_code": ref["index_code"],
                "cash_index_name": ref["index_name"],
                "cash_index_close": spot_close,
                "cash_index_change_pct": _num(index.get("change_pct")),
                "cash_index_turnover_cny": _num(index.get("turnover_cny")),
                "cash_basis_points": basis_points,
                "cash_basis_pct": basis_pct,
                "annualized_cash_basis_pct_inferred": annualized_basis,
                "cash_basis_note": (
                    "main future close minus cash-index close; annualization uses "
                    "calendar DTE and is not dividend/carry adjusted"
                ),
                "reference_etf_code": ref["etf_code"],
                "reference_etf_close": _num(etf.get("close")),
                "reference_etf_total_shares": _num(etf.get("total_shares")),
                "reference_etf_previous_total_shares": _num(
                    etf.get("previous_total_shares")
                ),
                "reference_etf_share_change": _num(etf.get("share_change")),
                "reference_etf_share_change_pct": _num(etf.get("share_change_pct")),
                "reference_etf_estimated_net_creation_redemption_cny": _num(
                    etf.get("estimated_net_creation_redemption_cny")
                ),
                "reference_etf_flow_method": etf.get("flow_method"),
            }
        )
