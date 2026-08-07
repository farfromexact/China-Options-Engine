from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
import requests
from bs4 import BeautifulSoup
from scipy.optimize import brentq
from scipy.stats import norm

TZ_CN = timezone(timedelta(hours=8))
PRODUCTS = {
    "HO": {"slug": "ho", "list_url": "https://stock.finance.sina.com.cn/futures/view/optionsCffexDP.php/ho/cffex"},
    "IO": {"slug": "io", "list_url": "https://stock.finance.sina.com.cn/futures/view/optionsCffexDP.php"},
    "MO": {"slug": "mo", "list_url": "https://stock.finance.sina.com.cn/futures/view/optionsCffexDP.php/mo/cffex"},
}
CHAIN_URL = "https://stock.finance.sina.com.cn/futures/api/openapi.php/OptionService.getOptionData"
DAYLINE_URL = "https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20_{symbol}{ymd}=/FutureOptionAllService.getOptionDayline"
CONTRACT_MULTIPLIER = 100.0
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.015"))
MAX_VOLUME_CONTRACTS = int(os.getenv("MAX_VOLUME_CONTRACTS", "320"))
REQ_TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 China-Options-Engine/1.0"}
DATA_DIR = Path("data")
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LATEST_PATH = DATA_DIR / "latest.json"


def get(url: str, *, params: dict[str, Any] | None = None) -> requests.Response:
    r = requests.get(url, params=params, headers=HEADERS, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def expiry_from_symbol(symbol: str) -> date:
    m = re.search(r"(\d{4})$", symbol.lower())
    if not m:
        raise ValueError(f"Cannot infer expiry from {symbol}")
    yymm = m.group(1)
    yy, mm = int(yymm[:2]), int(yymm[2:])
    return third_friday(2000 + yy, mm)


def year_fraction(expiry: date, now: datetime) -> float:
    expiry_dt = datetime.combine(expiry, time(15, 0), tzinfo=TZ_CN)
    return max((expiry_dt - now).total_seconds() / (365.0 * 86400.0), 1.0 / (365.0 * 24.0))


def list_contracts(product: str) -> list[str]:
    cfg = PRODUCTS[product]
    soup = BeautifulSoup(get(cfg["list_url"]).text, "lxml")
    ul = soup.find(attrs={"id": "option_suffix"})
    if ul is None:
        raise RuntimeError(f"{product}: option_suffix not found")
    vals = [li.get_text(strip=True).lower() for li in ul.find_all("li")]
    vals = [v for v in vals if re.fullmatch(fr"{cfg['slug']}\d{{4}}", v)]
    # Preserve source order, remove duplicates.
    return list(dict.fromkeys(vals))


def parse_num(x: Any) -> float | None:
    try:
        if x in (None, "", "--"):
            return None
        v = float(x)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return None


def fetch_chain(product: str, expiry_symbol: str) -> list[dict[str, Any]]:
    slug = PRODUCTS[product]["slug"]
    r = get(CHAIN_URL, params={"type": "futures", "product": slug, "exchange": "cffex", "pinzhong": expiry_symbol})
    text = r.text
    payload = json.loads(text[text.find("{") : text.rfind("}") + 1])
    data = payload["result"]["data"]
    calls, puts = data.get("up", []), data.get("down", [])
    rows: list[dict[str, Any]] = []
    for c, p in zip(calls, puts):
        if len(c) < 9 or len(p) < 8:
            continue
        strike = parse_num(c[7])
        if strike is None:
            continue
        rows.append({
            "strike": strike,
            "call": {"bid_size": parse_num(c[0]), "bid": parse_num(c[1]), "last": parse_num(c[2]), "ask": parse_num(c[3]), "ask_size": parse_num(c[4]), "oi": parse_num(c[5]), "change": parse_num(c[6]), "symbol": str(c[8]).strip()},
            "put": {"bid_size": parse_num(p[0]), "bid": parse_num(p[1]), "last": parse_num(p[2]), "ask": parse_num(p[3]), "ask_size": parse_num(p[4]), "oi": parse_num(p[5]), "change": parse_num(p[6]), "symbol": str(p[7]).strip()},
        })
    return rows


def mid(q: dict[str, Any]) -> float | None:
    b, a = q.get("bid"), q.get("ask")
    if b is None or a is None or b <= 0 or a <= 0 or a < b:
        return None
    # Reject extremely wide markets.
    m = 0.5 * (a + b)
    if m <= 0 or (a - b) / m > 0.60:
        return None
    return m


def infer_forward(rows: list[dict[str, Any]], t: float, r: float) -> float | None:
    candidates = []
    er = math.exp(r * t)
    for row in rows:
        c, p = mid(row["call"]), mid(row["put"])
        if c is None or p is None:
            continue
        k = row["strike"]
        f = k + er * (c - p)
        if f > 0:
            spread = abs(c - p) / max(c + p, 1e-9)
            candidates.append((spread, f))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    core = [f for _, f in candidates[: max(3, min(9, len(candidates)))]]
    return float(median(core))


def black76_price(cp: str, f: float, k: float, t: float, r: float, vol: float) -> float:
    if t <= 0 or vol <= 0:
        return math.exp(-r * t) * max((f - k) if cp == "C" else (k - f), 0.0)
    s = vol * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * vol * vol * t) / s
    d2 = d1 - s
    df = math.exp(-r * t)
    if cp == "C":
        return df * (f * norm.cdf(d1) - k * norm.cdf(d2))
    return df * (k * norm.cdf(-d2) - f * norm.cdf(-d1))


def implied_vol(cp: str, price: float, f: float, k: float, t: float, r: float) -> float | None:
    intrinsic = math.exp(-r * t) * max((f-k) if cp == "C" else (k-f), 0.0)
    if price <= intrinsic + 1e-8:
        return None
    try:
        return float(brentq(lambda v: black76_price(cp, f, k, t, r, v) - price, 1e-4, 5.0, maxiter=100))
    except Exception:
        return None


def greeks(cp: str, f: float, k: float, t: float, r: float, vol: float) -> tuple[float, float]:
    s = vol * math.sqrt(t)
    d1 = (math.log(f/k) + 0.5*vol*vol*t) / s
    df = math.exp(-r*t)
    delta = df * norm.cdf(d1) if cp == "C" else -df * norm.cdf(-d1)
    gamma = df * norm.pdf(d1) / (f * vol * math.sqrt(t))
    return float(delta), float(gamma)


def add_analytics(rows: list[dict[str, Any]], expiry: date, now: datetime) -> dict[str, Any]:
    t = year_fraction(expiry, now)
    f = infer_forward(rows, t, RISK_FREE_RATE)
    if f is None:
        return {"expiry": expiry.isoformat(), "t_years": t, "forward": None, "rows": rows, "metrics": {}}

    for row in rows:
        for cp, key in (("C", "call"), ("P", "put")):
            q = row[key]
            px = mid(q)
            q["mid"] = px
            q["iv"] = None
            q["delta"] = None
            q["gamma"] = None
            q["gamma_oi"] = None
            q["gamma_1pct"] = None
            if px is None:
                continue
            iv = implied_vol(cp, px, f, row["strike"], t, RISK_FREE_RATE)
            if iv is None:
                continue
            delta, gamma = greeks(cp, f, row["strike"], t, RISK_FREE_RATE, iv)
            oi = q.get("oi") or 0.0
            q.update({"iv": iv, "delta": delta, "gamma": gamma,
                      "gamma_oi": gamma * oi * CONTRACT_MULTIPLIER,
                      "gamma_1pct": gamma * oi * CONTRACT_MULTIPLIER * f * f * 0.01})

    points = []
    for row in rows:
        for cp, key in (("C", "call"), ("P", "put")):
            q = row[key]
            if q.get("iv") is not None and q.get("delta") is not None:
                points.append({"cp": cp, "strike": row["strike"], "iv": q["iv"], "delta": q["delta"]})

    def nearest_iv(cp: str, target_delta: float) -> float | None:
        pts = [x for x in points if x["cp"] == cp]
        if not pts:
            return None
        x = min(pts, key=lambda z: abs(z["delta"] - target_delta))
        return float(x["iv"])

    # ATM uses option with |delta| closest to 0.50 across both sides.
    atm_candidates = [x for x in points if 0.30 <= abs(x["delta"]) <= 0.70]
    atm_iv = None
    if atm_candidates:
        # Average nearest call/put ATM where available.
        c = nearest_iv("C", 0.50)
        p = nearest_iv("P", -0.50)
        vals = [x for x in (c, p) if x is not None]
        atm_iv = float(sum(vals) / len(vals)) if vals else None

    c25, p25 = nearest_iv("C", 0.25), nearest_iv("P", -0.25)
    c10, p10 = nearest_iv("C", 0.10), nearest_iv("P", -0.10)
    rr25 = (c25 - p25) if c25 is not None and p25 is not None else None
    bf25 = ((c25 + p25) / 2 - atm_iv) if c25 is not None and p25 is not None and atm_iv is not None else None
    call_oi = sum((row["call"].get("oi") or 0) for row in rows)
    put_oi = sum((row["put"].get("oi") or 0) for row in rows)
    pcr_oi = put_oi / call_oi if call_oi > 0 else None

    gamma_by_strike = []
    for row in rows:
        g = abs(row["call"].get("gamma_1pct") or 0) + abs(row["put"].get("gamma_1pct") or 0)
        gamma_by_strike.append({"strike": row["strike"], "abs_gamma_1pct": g})
    gamma_by_strike.sort(key=lambda x: x["abs_gamma_1pct"], reverse=True)

    return {
        "expiry": expiry.isoformat(), "t_years": t, "forward": f, "rows": rows,
        "metrics": {
            "atm_iv": atm_iv, "call25_iv": c25, "put25_iv": p25, "call10_iv": c10, "put10_iv": p10,
            "rr25": rr25, "bf25": bf25, "call_oi": call_oi, "put_oi": put_oi, "pcr_oi": pcr_oi,
            "gamma_peaks": gamma_by_strike[:5],
        },
    }


def fetch_day_volume(symbol: str) -> float | None:
    now = datetime.now(TZ_CN)
    ymd = f"{now.year}_{now.month}_{now.day}"
    url = DAYLINE_URL.format(symbol=symbol, ymd=ymd)
    try:
        text = get(url, params={"symbol": symbol}).text
        raw = text[text.find("[") : text.rfind("]") + 1]
        arr = json.loads(raw.replace("'", '"'))
        if not arr:
            return None
        last = arr[-1]
        # AKShare mapping: open, high, low, close, volume, date
        return parse_num(last[4]) if len(last) >= 6 else None
    except Exception:
        return None


def enrich_volume(products: dict[str, Any]) -> None:
    candidates = []
    for product, pdata in products.items():
        expiries = pdata.get("expiries", [])[:2]  # nearest two expiries first
        for exp in expiries:
            for row in exp.get("rows", []):
                for key in ("call", "put"):
                    q = row[key]
                    candidates.append((q.get("oi") or 0, q))
    candidates.sort(key=lambda x: x[0], reverse=True)
    for _, q in candidates[:MAX_VOLUME_CONTRACTS]:
        sym = q.get("symbol")
        if sym:
            q["volume"] = fetch_day_volume(sym)

    for pdata in products.values():
        for exp in pdata.get("expiries", []):
            call_vol = sum((row["call"].get("volume") or 0) for row in exp.get("rows", []))
            put_vol = sum((row["put"].get("volume") or 0) for row in exp.get("rows", []))
            exp["metrics"]["call_volume_partial"] = call_vol
            exp["metrics"]["put_volume_partial"] = put_vol
            exp["metrics"]["pcr_volume_partial"] = (put_vol / call_vol) if call_vol > 0 else None
            exp["metrics"]["volume_scope"] = f"top {MAX_VOLUME_CONTRACTS} contracts by OI across nearest two expiries; partial"


def load_previous(today: date) -> dict[str, Any] | None:
    files = sorted(SNAPSHOT_DIR.glob("*.json"), reverse=True)
    for p in files:
        if p.stem < today.isoformat():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return None


def add_changes(current: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if not previous:
        current["previous_date"] = None
        return
    current["previous_date"] = previous.get("date")
    prev_map = previous.get("products", {})
    for product, pdata in current["products"].items():
        prev_exps = {x.get("symbol"): x for x in prev_map.get(product, {}).get("expiries", [])}
        for exp in pdata.get("expiries", []):
            old = prev_exps.get(exp.get("symbol"))
            if not old:
                continue
            m, om = exp["metrics"], old.get("metrics", {})
            for key in ("atm_iv", "rr25", "pcr_oi", "call_oi", "put_oi"):
                if m.get(key) is not None and om.get(key) is not None:
                    m[f"{key}_change_1d"] = m[key] - om[key]


def main() -> None:
    now = datetime.now(TZ_CN)
    today = now.date()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "date": today.isoformat(),
        "generated_at": now.isoformat(),
        "source": "Sina CFFEX option chain; methodology mirrors AKShare public implementation",
        "assumptions": {
            "risk_free_rate": RISK_FREE_RATE,
            "expiry_calendar": "third Friday inferred from YYMM symbol; holiday adjustment not yet connected to CFFEX calendar",
            "iv_model": "Black-76 on put-call-parity implied forward",
            "iv_price": "bid/ask mid when both sides valid; quotes with >60% relative spread filtered",
            "dealer_gex": "not reported as fact; only absolute gamma concentration is produced in v1",
        },
        "products": {},
        "errors": [],
    }

    for product in PRODUCTS:
        pdata = {"expiries": []}
        result["products"][product] = pdata
        try:
            symbols = list_contracts(product)
        except Exception as e:
            result["errors"].append(f"{product} list_contracts: {e}")
            continue
        for sym in symbols:
            try:
                expiry = expiry_from_symbol(sym)
                if expiry < today:
                    continue
                rows = fetch_chain(product, sym)
                analyzed = add_analytics(rows, expiry, now)
                analyzed["symbol"] = sym.upper()
                pdata["expiries"].append(analyzed)
            except Exception as e:
                result["errors"].append(f"{product} {sym}: {e}")

    enrich_volume(result["products"])
    previous = load_previous(today)
    add_changes(result, previous)

    snapshot = SNAPSHOT_DIR / f"{today.isoformat()}.json"
    snapshot.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    LATEST_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"date": result["date"], "products": {k: len(v["expiries"]) for k, v in result["products"].items()}, "errors": result["errors"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
