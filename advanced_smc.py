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


def pivots(cs: list[dict], strength: int = 2, window: int = 120):
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
        return {"bias": "NEUTRAL", "trend": "UNKNOWN", "bos": None, "highs": hi[-3:], "lows": lo[-3:]}
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


def dealing_range(cs: list[dict], lookback: int = 80) -> dict[str, Any]:
    d = cs[-lookback:]
    high = max(x["high"] for x in d)
    low = min(x["low"] for x in d)
    eq = (high + low) / 2
    return {"high": high, "low": low, "equilibrium": eq, "premium": eq, "discount": eq}


def equal_liquidity(cs: list[dict], tolerance: float = 0.0015) -> dict[str, list[float]]:
    hi, lo = pivots(cs, strength=2, window=100)
    eqh, eql = [], []
    for arr, out in ((hi, eqh), (lo, eql)):
        for i in range(1, len(arr)):
            if abs(arr[i][1] - arr[i-1][1]) / max(arr[i-1][1], 1) <= tolerance:
                out.append((arr[i][1] + arr[i-1][1]) / 2)
    return {"equal_highs": eqh[-3:], "equal_lows": eql[-3:]}


def sweep(cs: list[dict], lookback: int = 24) -> dict[str, Any]:
    if len(cs) < lookback + 2:
        return {"type": "NONE", "level": None, "quality": 0}
    cur = cs[-1]
    prior = cs[-lookback-1:-1]
    ph = max(x["high"] for x in prior)
    pl = min(x["low"] for x in prior)
    if cur["low"] < pl and cur["close"] > pl:
        q = min(100, 60 + int((pl-cur["low"]) / max(rng(cur), 1e-9) * 40))
        return {"type": "SELL_SIDE_LIQUIDITY_SWEEP", "level": pl, "quality": q}
    if cur["high"] > ph and cur["close"] < ph:
        q = min(100, 60 + int((cur["high"]-ph) / max(rng(cur), 1e-9) * 40))
        return {"type": "BUY_SIDE_LIQUIDITY_SWEEP", "level": ph, "quality": q}
    return {"type": "NONE", "level": None, "quality": 0}


def displacement(cs: list[dict], multiplier: float = 1.7) -> dict[str, Any]:
    if len(cs) < 14:
        return {"confirmed": False, "score": 0}
    cur = cs[-1]
    avg = mean(body(x) for x in cs[-13:-1])
    ratio = body(cur) / max(avg, 1e-12)
    directional = bull(cur) or bear(cur)
    close_strength = (cur["close"] - cur["low"]) / rng(cur) if bull(cur) else (cur["high"] - cur["close"]) / rng(cur)
    ok = ratio >= multiplier and directional and close_strength >= 0.65
    return {"confirmed": ok, "ratio": round(ratio, 2), "close_strength": round(close_strength, 2), "score": min(100, int(ratio / multiplier * 70 + close_strength * 30))}


def mss(cs: list[dict], lookback: int = 10) -> dict[str, Any]:
    prior = cs[-lookback-1:-1]
    cur = cs[-1]
    hi = max(x["high"] for x in prior)
    lo = min(x["low"] for x in prior)
    if cur["close"] > hi:
        return {"type": "BULLISH_MSS", "level": hi}
    if cur["close"] < lo:
        return {"type": "BEARISH_MSS", "level": lo}
    return {"type": "NONE", "level": None}


def fvg(cs: list[dict], direction: str, scan: int = 40) -> dict[str, Any] | None:
    d = cs[-scan:]
    for i in range(len(d)-1, 1, -1):
        a, mid, c = d[i-2], d[i-1], d[i]
        if direction == "LONG" and a["high"] < c["low"] and bull(mid):
            return {"type": "BULLISH_FVG", "low": a["high"], "high": c["low"], "time": c["time"]}
        if direction == "SHORT" and a["low"] > c["high"] and bear(mid):
            return {"type": "BEARISH_FVG", "low": c["high"], "high": a["low"], "time": c["time"]}
    return None


def order_block(cs: list[dict], direction: str, scan: int = 20) -> dict[str, Any] | None:
    d = cs[-scan:]
    for i in range(len(d)-2, -1, -1):
        c = d[i]
        if direction == "LONG" and bear(c):
            return {"type": "BULLISH_ORDER_BLOCK", "low": c["low"], "high": c["open"], "time": c["time"]}
        if direction == "SHORT" and bull(c):
            return {"type": "BEARISH_ORDER_BLOCK", "low": c["open"], "high": c["high"], "time": c["time"]}
    return None


def breaker(cs: list[dict], direction: str, scan: int = 30) -> dict[str, Any] | None:
    d = cs[-scan:]
    # A breaker is treated conservatively: the last opposite candle whose level was
    # decisively broken by a later displacement candle.
    for i in range(len(d)-2, 0, -1):
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
        return {"type": "CONFLUENCE_POI", "low": low, "high": high, "components": [x["type"] for x in z]}
    # No exact overlap: use the most recent FVG/OB rather than inventing a zone.
    return z[0]


def ote(mid: float, low: float, high: float, direction: str) -> bool:
    # 62-79% retracement of the impulse proxy (POI itself is the execution zone).
    if high <= low:
        return False
    width = high - low
    if direction == "LONG":
        return low <= mid <= low + width * 0.79
    return high - width * 0.79 <= mid <= high


def target_liquidity(cs: list[dict], direction: str, entry: float) -> float | None:
    hi, lo = pivots(cs, strength=2, window=140)
    if direction == "LONG":
        candidates = [x[1] for x in hi if x[1] > entry]
        return min(candidates) if candidates else None
    candidates = [x[1] for x in lo if x[1] < entry]
    return max(candidates) if candidates else None


def analyze_advanced(h4: list[dict], h1: list[dict], m15: list[dict], m5: list[dict], live_price: float) -> dict[str, Any]:
    s4, s1 = structure(h4), structure(h1)
    dr = dealing_range(h1)
    liq = equal_liquidity(m15)
    sw = sweep(m15)
    ms = mss(m5)
    disp = displacement(m5)
    direction = "LONG" if s4["bias"] == "BULLISH" and s1["bias"] == "BULLISH" else "SHORT" if s4["bias"] == "BEARISH" and s1["bias"] == "BEARISH" else "NONE"
    if direction == "NONE":
        return {"setup": "WAIT", "action": "WAIT", "direction": "NONE", "score": 0, "reason": "4H/1H not aligned", "live_price": live_price, "4H": s4, "1H": s1, "15M": {"liquidity": sw, "equal_liquidity": liq}, "5M": {"MSS": ms, "displacement": disp}}

    expected_sw = "SELL_SIDE_LIQUIDITY_SWEEP" if direction == "LONG" else "BUY_SIDE_LIQUIDITY_SWEEP"
    expected_ms = "BULLISH_MSS" if direction == "LONG" else "BEARISH_MSS"
    fv = fvg(m5, direction)
    ob = order_block(m5, direction)
    br = breaker(m5, direction)
    poi = overlap([fv, ob, br])
    checks = {
        "HTF_alignment": True,
        "external_liquidity_sweep": sw["type"] == expected_sw,
        "MSS": ms["type"] == expected_ms,
        "displacement": disp["confirmed"],
        "FVG_or_OB": bool(fv or ob),
        "POI_confluence": bool(poi),
    }
    score = 0
    score += 20 if checks["HTF_alignment"] else 0
    score += 20 if checks["external_liquidity_sweep"] else 0
    score += 20 if checks["MSS"] else 0
    score += 15 if checks["displacement"] else 0
    score += 10 if checks["FVG_or_OB"] else 0
    score += 10 if checks["POI_confluence"] else 0
    score += 5 if (direction == "LONG" and live_price < dr["equilibrium"]) or (direction == "SHORT" and live_price > dr["equilibrium"]) else 0

    entry = ((poi["low"] + poi["high"]) / 2) if poi else None
    in_poi = bool(poi and poi["low"] <= live_price <= poi["high"])
    pd_ok = bool(entry and ((direction == "LONG" and entry <= dr["equilibrium"]) or (direction == "SHORT" and entry >= dr["equilibrium"])))
    if pd_ok:
        score += 5

    if direction == "LONG" and sw["level"]:
        sl = min(sw["level"], poi["low"] if poi else sw["level"]) * 0.999
    elif direction == "SHORT" and sw["level"]:
        sl = max(sw["level"], poi["high"] if poi else sw["level"]) * 1.001
    else:
        sl = None
    risk = abs(entry - sl) if entry is not None and sl is not None else None
    liq_target = target_liquidity(h1, direction, entry) if entry is not None else None
    tp1 = liq_target if liq_target is not None else (entry + 2*risk if direction == "LONG" and risk else entry - 2*risk if direction == "SHORT" and risk else None)
    tp2 = entry + 3*risk if direction == "LONG" and risk else entry - 3*risk if direction == "SHORT" and risk else None
    rr1 = abs(tp1-entry)/risk if tp1 is not None and risk else None

    valid = score >= 85 and all(checks.values()) and pd_ok and poi is not None and risk is not None and risk > 0
    action = "ENTER_ON_RETRACE" if valid and in_poi else "WAIT_FOR_RETRACE" if valid else "WAIT"
    reason = "ADVANCED_SMC_CONFIRMED" if valid else "; ".join(k for k,v in checks.items() if not v) or "Premium/discount or POI condition not confirmed"
    return {
        "setup": "VALID" if valid else "WAIT", "action": action, "direction": direction, "score": score,
        "reason": reason, "live_price": live_price, "entry_zone": {"low": poi["low"], "high": poi["high"]} if poi else {"low": None, "high": None},
        "stop_loss": sl, "take_profit_1": tp1, "take_profit_2": tp2, "risk_reward": {"TP1": rr1, "TP2": 3.0} if risk else None,
        "dealing_range": dr, "liquidity": liq, "checks": checks, "4H": s4, "1H": s1,
        "15M": {"liquidity_sweep": sw, "equal_liquidity": liq}, "5M": {"MSS": ms, "displacement": disp, "FVG": fv, "order_block": ob, "breaker": br},
        "POI": poi,
    }
