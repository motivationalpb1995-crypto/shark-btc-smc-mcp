from __future__ import annotations

from statistics import mean
from typing import Any


def body(c: dict) -> float:
    return abs(c["close"] - c["open"])


def rng(c: dict) -> float:
    return max(c["high"] - c["low"], 1e-12)


def bull(c: dict) -> bool:
    return c["close"] > c["open"]


def bear(c: dict) -> bool:
    return c["close"] < c["open"]


def atr(cs: list[dict], period: int = 14) -> float:
    if len(cs) < period + 1:
        return mean(rng(x) for x in cs[-min(len(cs), period):])
    trs = []
    for i in range(1, len(cs)):
        trs.append(max(cs[i]["high"] - cs[i]["low"], abs(cs[i]["high"] - cs[i-1]["close"]), abs(cs[i]["low"] - cs[i-1]["close"])))
    return mean(trs[-period:])


def pivots(cs: list[dict], strength: int = 2, window: int = 180):
    d = cs[-window:]
    hi, lo = [], []
    for i in range(strength, len(d) - strength):
        h, l = d[i]["high"], d[i]["low"]
        if all(h > d[j]["high"] for j in range(i-strength, i)) and all(h >= d[j]["high"] for j in range(i+1, i+strength+1)):
            hi.append((i, h, d[i]["time"]))
        if all(l < d[j]["low"] for j in range(i-strength, i)) and all(l <= d[j]["low"] for j in range(i+1, i+strength+1)):
            lo.append((i, l, d[i]["time"]))
    return hi, lo


def structure(cs: list[dict]) -> dict[str, Any]:
    hi, lo = pivots(cs)
    if len(hi) < 3 or len(lo) < 3:
        return {"bias": "NEUTRAL", "trend": "UNKNOWN", "bos": None, "swing_high": None, "swing_low": None, "highs": hi[-3:], "lows": lo[-3:]}
    hh = hi[-1][1] > hi[-2][1]
    hl = lo[-1][1] > lo[-2][1]
    lh = hi[-1][1] < hi[-2][1]
    ll = lo[-1][1] < lo[-2][1]
    if hh and hl:
        trend = bias = "BULLISH"
    elif lh and ll:
        trend = bias = "BEARISH"
    else:
        trend = bias = "RANGE"
    close = cs[-1]["close"]
    bos = "BULLISH_BOS" if close > hi[-1][1] else "BEARISH_BOS" if close < lo[-1][1] else None
    return {"bias": bias, "trend": trend, "bos": bos, "swing_high": hi[-1][1], "swing_low": lo[-1][1], "highs": hi[-3:], "lows": lo[-3:]}


def dealing_range(cs: list[dict], lookback: int = 120) -> dict[str, Any]:
    d = cs[-lookback:]
    high = max(x["high"] for x in d)
    low = min(x["low"] for x in d)
    eq = (high + low) / 2
    return {
        "high": high,
        "low": low,
        "equilibrium": eq,
        "premium_zone": {"low": eq, "high": high},
        "discount_zone": {"low": low, "high": eq},
    }


def equal_liquidity(cs: list[dict], tolerance: float = 0.0012) -> dict[str, list[float]]:
    hi, lo = pivots(cs, strength=2, window=140)
    eqh, eql = [], []
    for arr, out in ((hi, eqh), (lo, eql)):
        for i in range(1, len(arr)):
            if abs(arr[i][1] - arr[i-1][1]) / max(abs(arr[i-1][1]), 1) <= tolerance:
                out.append((arr[i][1] + arr[i-1][1]) / 2)
    return {"equal_highs": eqh[-5:], "equal_lows": eql[-5:]}


def sweep(cs: list[dict], lookback: int = 30, max_age: int = 3) -> dict[str, Any]:
    """Detect a recent liquidity raid followed by a close back inside the range."""
    if len(cs) < lookback + max_age + 2:
        return {"type": "NONE", "level": None, "extreme": None, "quality": 0, "bars_ago": None}
    for age in range(max_age):
        idx = len(cs) - 1 - age
        cur = cs[idx]
        prior = cs[max(0, idx-lookback):idx]
        if len(prior) < lookback:
            continue
        ph = max(x["high"] for x in prior)
        pl = min(x["low"] for x in prior)
        if cur["low"] < pl and cur["close"] > pl:
            q = min(100, 65 + int((pl-cur["low"]) / rng(cur) * 35))
            return {"type": "SELL_SIDE_LIQUIDITY_SWEEP", "level": pl, "extreme": cur["low"], "quality": q, "bars_ago": age}
        if cur["high"] > ph and cur["close"] < ph:
            q = min(100, 65 + int((cur["high"]-ph) / rng(cur) * 35))
            return {"type": "BUY_SIDE_LIQUIDITY_SWEEP", "level": ph, "extreme": cur["high"], "quality": q, "bars_ago": age}
    return {"type": "NONE", "level": None, "extreme": None, "quality": 0, "bars_ago": None}


def displacement(cs: list[dict], multiplier: float = 1.8) -> dict[str, Any]:
    if len(cs) < 20:
        return {"confirmed": False, "score": 0, "ratio": 0, "close_strength": 0, "direction": "NONE"}
    cur = cs[-1]
    avg_body = mean(body(x) for x in cs[-16:-1])
    ratio = body(cur) / max(avg_body, 1e-12)
    close_strength = (cur["close"] - cur["low"]) / rng(cur) if bull(cur) else (cur["high"] - cur["close"]) / rng(cur)
    direction = "BULLISH" if bull(cur) else "BEARISH" if bear(cur) else "NONE"
    ok = direction != "NONE" and ratio >= multiplier and close_strength >= 0.70 and body(cur) / rng(cur) >= 0.55
    return {"confirmed": ok, "ratio": round(ratio, 2), "close_strength": round(close_strength, 2), "score": min(100, int(ratio / multiplier * 65 + close_strength * 35)), "direction": direction}


def mss(cs: list[dict], lookback: int = 12) -> dict[str, Any]:
    """Confirmed close through the most recent short-term swing range."""
    if len(cs) < lookback + 2:
        return {"type": "NONE", "level": None, "bars_ago": None}
    for age in range(3):
        idx = len(cs) - 1 - age
        cur = cs[idx]
        prior = cs[idx-lookback:idx]
        hi = max(x["high"] for x in prior)
        lo = min(x["low"] for x in prior)
        if cur["close"] > hi:
            return {"type": "BULLISH_MSS", "level": hi, "bars_ago": age}
        if cur["close"] < lo:
            return {"type": "BEARISH_MSS", "level": lo, "bars_ago": age}
    return {"type": "NONE", "level": None, "bars_ago": None}


def fvg(cs: list[dict], direction: str, scan: int = 60) -> dict[str, Any] | None:
    d = cs[-scan:]
    for i in range(len(d)-1, 1, -1):
        a, mid, c = d[i-2], d[i-1], d[i]
        if direction == "LONG" and a["high"] < c["low"] and bull(mid):
            return {"type": "BULLISH_FVG", "low": a["high"], "high": c["low"], "time": c["time"], "mid": (a["high"] + c["low"]) / 2}
        if direction == "SHORT" and a["low"] > c["high"] and bear(mid):
            return {"type": "BEARISH_FVG", "low": c["high"], "high": a["low"], "time": c["time"], "mid": (c["high"] + a["low"]) / 2}
    return None


def order_block(cs: list[dict], direction: str, scan: int = 35) -> dict[str, Any] | None:
    """Use the last opposite candle that immediately precedes a strong move."""
    d = cs[-scan:]
    for i in range(len(d)-2, 0, -1):
        c = d[i]
        nxt = d[i+1]
        if direction == "LONG" and bear(c) and bull(nxt) and body(nxt) >= 1.4 * max(body(c), 1e-12):
            return {"type": "BULLISH_ORDER_BLOCK", "low": c["low"], "high": max(c["open"], c["close"]), "time": c["time"]}
        if direction == "SHORT" and bull(c) and bear(nxt) and body(nxt) >= 1.4 * max(body(c), 1e-12):
            return {"type": "BEARISH_ORDER_BLOCK", "low": min(c["open"], c["close"]), "high": c["high"], "time": c["time"]}
    return None


def breaker(cs: list[dict], direction: str, scan: int = 40) -> dict[str, Any] | None:
    d = cs[-scan:]
    for i in range(len(d)-3, 0, -1):
        c, later = d[i], d[i+1]
        if direction == "LONG" and bear(c) and later["close"] > c["high"]:
            return {"type": "BULLISH_BREAKER", "low": c["low"], "high": c["high"], "time": c["time"]}
        if direction == "SHORT" and bull(c) and later["close"] < c["low"]:
            return {"type": "BEARISH_BREAKER", "low": c["low"], "high": c["high"], "time": c["time"]}
    return None


def overlap(zones: list[dict | None]) -> dict[str, Any] | None:
    z = [x for x in zones if x]
    if not z:
        return None
    low = max(x["low"] for x in z)
    high = min(x["high"] for x in z)
    if low < high:
        return {"type": "CONFLUENCE_POI", "low": low, "high": high, "mid": (low + high) / 2, "components": [x["type"] for x in z]}
    # If there is no exact overlap, keep the most recently detected POI rather than creating a fake intersection.
    return {**z[0], "components": [z[0]["type"]]}


def impulse(cs: list[dict], direction: str, lookback: int = 30) -> dict[str, float] | None:
    d = cs[-lookback:]
    if direction == "LONG":
        low_i = min(range(len(d)), key=lambda i: d[i]["low"])
        if low_i == len(d)-1:
            return None
        high = max(x["high"] for x in d[low_i:])
        return {"low": d[low_i]["low"], "high": high}
    if direction == "SHORT":
        high_i = max(range(len(d)), key=lambda i: d[i]["high"])
        if high_i == len(d)-1:
            return None
        low = min(x["low"] for x in d[high_i:])
        return {"low": low, "high": d[high_i]["high"]}
    return None


def ote_ok(price: float, imp: dict[str, float] | None, direction: str) -> bool:
    if not imp or imp["high"] <= imp["low"]:
        return False
    width = imp["high"] - imp["low"]
    if direction == "LONG":
        return imp["high"] - width * 0.79 <= price <= imp["high"] - width * 0.62
    return imp["low"] + width * 0.62 <= price <= imp["low"] + width * 0.79


def target_liquidity(cs: list[dict], direction: str, entry: float) -> list[float]:
    hi, lo = pivots(cs, strength=2, window=180)
    if direction == "LONG":
        return sorted({x[1] for x in hi if x[1] > entry})
    return sorted({x[1] for x in lo if x[1] < entry}, reverse=True)


def analyze_advanced(h4: list[dict], h1: list[dict], m15: list[dict], m5: list[dict], live_price: float) -> dict[str, Any]:
    s4, s1 = structure(h4), structure(h1)
    dr = dealing_range(h1)
    liq15 = equal_liquidity(m15)
    sw15 = sweep(m15)
    ms5 = mss(m5)
    disp5 = displacement(m5)

    if s4["bias"] == "BULLISH" and s1["bias"] == "BULLISH":
        direction = "LONG"
    elif s4["bias"] == "BEARISH" and s1["bias"] == "BEARISH":
        direction = "SHORT"
    else:
        direction = "NONE"

    base = {
        "live_price": live_price,
        "4H": s4,
        "1H": s1,
        "15M": {"liquidity_sweep": sw15, "equal_liquidity": liq15},
        "5M": {"MSS": ms5, "displacement": disp5},
        "dealing_range": dr,
    }
    if direction == "NONE":
        return {"setup": "WAIT", "action": "WAIT", "direction": "NONE", "score": 0, "reason": "4H and 1H structure are not aligned", **base}

    expected_sw = "SELL_SIDE_LIQUIDITY_SWEEP" if direction == "LONG" else "BUY_SIDE_LIQUIDITY_SWEEP"
    expected_ms = "BULLISH_MSS" if direction == "LONG" else "BEARISH_MSS"
    expected_disp = "BULLISH" if direction == "LONG" else "BEARISH"

    fv = fvg(m5, direction)
    ob = order_block(m5, direction)
    br = breaker(m5, direction)
    poi = overlap([fv, ob, br])
    imp = impulse(m5, direction)
    entry = poi["mid"] if poi else None

    checks = {
        "HTF_alignment": True,
        "liquidity_sweep": sw15["type"] == expected_sw,
        "MSS": ms5["type"] == expected_ms,
        "displacement": disp5["confirmed"] and disp5["direction"] == expected_disp,
        "FVG_or_OB": bool(fv or ob),
        "POI_confluence": bool(poi),
        "correct_PD": bool(entry is not None and ((direction == "LONG" and entry <= dr["equilibrium"]) or (direction == "SHORT" and entry >= dr["equilibrium"]))),
        "OTE": bool(entry is not None and ote_ok(entry, imp, direction)),
    }

    weights = {"HTF_alignment": 20, "liquidity_sweep": 18, "MSS": 18, "displacement": 14, "FVG_or_OB": 8, "POI_confluence": 7, "correct_PD": 8, "OTE": 7}
    score = sum(weights[k] for k, ok in checks.items() if ok)

    # Professional execution: stop beyond the liquidity raid/POI with an ATR buffer.
    a = atr(m5)
    if direction == "LONG" and entry is not None:
        anchor = min(x for x in [sw15.get("extreme"), poi.get("low") if poi else None] if x is not None)
        sl = anchor - max(a * 0.15, entry * 0.0002)
    elif direction == "SHORT" and entry is not None:
        anchor = max(x for x in [sw15.get("extreme"), poi.get("high") if poi else None] if x is not None)
        sl = anchor + max(a * 0.15, entry * 0.0002)
    else:
        sl = None

    risk = abs(entry - sl) if entry is not None and sl is not None else None
    targets = target_liquidity(h1, direction, entry) if entry is not None else []
    valid_targets = [x for x in targets if risk and abs(x-entry) / risk >= 2.0]
    tp1 = valid_targets[0] if valid_targets else ((entry + 2*risk) if direction == "LONG" and risk else (entry - 2*risk) if direction == "SHORT" and risk else None)
    tp2 = valid_targets[1] if len(valid_targets) > 1 else ((entry + 3*risk) if direction == "LONG" and risk else (entry - 3*risk) if direction == "SHORT" and risk else None)
    rr1 = abs(tp1-entry) / risk if tp1 is not None and risk else None
    rr2 = abs(tp2-entry) / risk if tp2 is not None and risk else None
    rr_ok = bool(rr1 is not None and rr1 >= 2.0)
    in_poi = bool(poi and poi["low"] <= live_price <= poi["high"])

    # A signal is VALID only when the full institutional-style sequence is present.
    mandatory = all(checks.values()) and rr_ok and risk is not None and risk > 0
    valid = score >= 90 and mandatory
    action = "ENTER_ON_RETRACE" if valid and in_poi else "WAIT_FOR_RETRACE" if valid else "WAIT"
    missing = [k for k, ok in checks.items() if not ok]
    reason = "PRO_SMC_CONFIRMED" if valid else ("Missing: " + ", ".join(missing) if missing else "Risk/reward or execution condition not confirmed")

    return {
        "setup": "VALID" if valid else "WAIT",
        "action": action,
        "direction": direction,
        "score": score,
        "reason": reason,
        "entry_zone": {"low": poi["low"], "high": poi["high"], "mid": entry} if poi else {"low": None, "high": None, "mid": None},
        "stop_loss": sl,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_reward": {"TP1": rr1, "TP2": rr2} if risk else None,
        "atr_5m": a,
        "checks": checks,
        "liquidity_targets": targets[:5],
        "POI": poi,
        "impulse": imp,
        **base,
    }
