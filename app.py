import os
from typing import Any

import httpx
from fastmcp import FastMCP

mcp = FastMCP("BTCUSDT Perpetual SMC")

# Public market-data only. No API keys are required for this SMC analysis.
DEFAULT_PAIR = os.getenv("BTC_PAIR", "BTCUSDT")
CONTRACT_TYPE = "PERPETUAL"
VALID_INTERVALS = {"5m", "15m", "1h", "4h"}
EXCHANGES = {"BINANCE", "BYBIT"}
BINANCE_BASE_URL = os.getenv("BINANCE_FAPI_URL", "https://fapi.binance.com")
BYBIT_BASE_URL = os.getenv("BYBIT_V5_URL", "https://api.bybit.com")


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict:
    response = await client.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected response from {url}: {data}")
    return data


async def fetch_klines(exchange: str, pair: str, interval: str, limit: int = 300) -> list[dict]:
    exchange = exchange.upper()
    if exchange not in EXCHANGES:
        raise ValueError("Exchange must be BINANCE or BYBIT")
    if interval not in VALID_INTERVALS:
        raise ValueError("Interval must be one of: 5m, 15m, 1h, 4h")

    limit = min(max(int(limit), 50), 1000)
    symbol = pair.upper()
    bybit_interval = {"5m": "5", "15m": "15", "1h": "60", "4h": "240"}[interval]

    async with httpx.AsyncClient(timeout=20) as client:
        if exchange == "BINANCE":
            data = await _get_json(client, f"{BINANCE_BASE_URL}/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
            rows = data
        else:
            data = await _get_json(client, f"{BYBIT_BASE_URL}/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit})
            if data.get("retCode") not in (None, 0):
                raise ValueError(f"Bybit error: {data.get('retMsg')}")
            rows = data.get("result", {}).get("list", [])

    candles: list[dict] = []
    for row in rows or []:
        try:
            if exchange == "BINANCE":
                candles.append({"time": int(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "endTime": int(row[6]), "volume": float(row[5])})
            else:
                candles.append({"time": int(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "endTime": int(row[0]), "volume": float(row[5])})
        except (IndexError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])
    if len(candles) < 50:
        raise ValueError(f"Not enough candles from {exchange} for {symbol} {interval}: {len(candles)}")
    return candles[:-1]


async def fetch_live_price(exchange: str, pair: str) -> float:
    exchange = exchange.upper()
    symbol = pair.upper()
    async with httpx.AsyncClient(timeout=10) as client:
        if exchange == "BINANCE":
            data = await _get_json(client, f"{BINANCE_BASE_URL}/fapi/v1/ticker/price", {"symbol": symbol})
            return float(data["price"])
        data = await _get_json(client, f"{BYBIT_BASE_URL}/v5/market/tickers", {"category": "linear", "symbol": symbol})
        return float(data["result"]["list"][0]["lastPrice"])


def candle_body(c: dict) -> float:
    return abs(c["close"] - c["open"])


def is_bull(c: dict) -> bool:
    return c["close"] > c["open"]


def is_bear(c: dict) -> bool:
    return c["close"] < c["open"]


def pivot_high(candles: list[dict], i: int, strength: int = 2) -> bool:
    if i < strength or i + strength >= len(candles):
        return False
    h = candles[i]["high"]
    return all(h > candles[j]["high"] for j in range(i - strength, i)) and all(h >= candles[j]["high"] for j in range(i + 1, i + strength + 1))


def pivot_low(candles: list[dict], i: int, strength: int = 2) -> bool:
    if i < strength or i + strength >= len(candles):
        return False
    l = candles[i]["low"]
    return all(l < candles[j]["low"] for j in range(i - strength, i)) and all(l <= candles[j]["low"] for j in range(i + 1, i + strength + 1))


def swing_points(candles: list[dict], strength: int = 2, window: int = 80) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    data = candles[-window:]
    highs = [(i, c["high"]) for i, c in enumerate(data) if pivot_high(data, i, strength)]
    lows = [(i, c["low"]) for i, c in enumerate(data) if pivot_low(data, i, strength)]
    return highs, lows


def structure(candles: list[dict]) -> dict:
    highs, lows = swing_points(candles)
    if len(highs) < 2 or len(lows) < 2:
        return {"bias": "NEUTRAL", "bos": None, "swing_high": None, "swing_low": None}
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    if h2[1] > h1[1] and l2[1] > l1[1]:
        bias = "BULLISH"
    elif h2[1] < h1[1] and l2[1] < l1[1]:
        bias = "BEARISH"
    else:
        bias = "RANGE"
    last_close = candles[-1]["close"]
    bos = "BULLISH_BOS" if last_close > h2[1] else "BEARISH_BOS" if last_close < l2[1] else None
    return {"bias": bias, "bos": bos, "swing_high": h2[1], "swing_low": l2[1]}


def liquidity_sweep(candles: list[dict], lookback: int = 20) -> dict:
    if len(candles) < lookback + 2:
        return {"type": "NONE", "level": None}
    current = candles[-1]
    prior = candles[-lookback - 1:-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)
    if current["low"] < prior_low and current["close"] > prior_low:
        return {"type": "SELL_SIDE_LIQUIDITY_SWEEP", "level": prior_low}
    if current["high"] > prior_high and current["close"] < prior_high:
        return {"type": "BUY_SIDE_LIQUIDITY_SWEEP", "level": prior_high}
    return {"type": "NONE", "level": None}


def displacement(candles: list[dict], multiplier: float = 1.5) -> bool:
    if len(candles) < 12:
        return False
    avg = sum(candle_body(c) for c in candles[-11:-1]) / 10
    return candle_body(candles[-1]) >= avg * multiplier


def mss(candles: list[dict], lookback: int = 8) -> dict:
    if len(candles) < lookback + 2:
        return {"type": "NONE", "level": None}
    current = candles[-1]
    prior = candles[-lookback - 1:-1]
    high = max(c["high"] for c in prior)
    low = min(c["low"] for c in prior)
    if current["close"] > high:
        return {"type": "BULLISH_MSS", "level": high}
    if current["close"] < low:
        return {"type": "BEARISH_MSS", "level": low}
    return {"type": "NONE", "level": None}


def find_fvg(candles: list[dict], direction: str, scan: int = 30) -> dict | None:
    data = candles[-scan:]
    for i in range(len(data) - 1, 1, -1):
        a, _, c = data[i - 2], data[i - 1], data[i]
        if direction == "LONG" and a["high"] < c["low"]:
            return {"type": "BULLISH_FVG", "low": a["high"], "high": c["low"], "time": c["time"]}
        if direction == "SHORT" and a["low"] > c["high"]:
            return {"type": "BEARISH_FVG", "low": c["high"], "high": a["low"], "time": c["time"]}
    return None


def find_order_block(candles: list[dict], direction: str, scan: int = 12) -> dict | None:
    data = candles[-scan:]
    for c in reversed(data[:-1]):
        if direction == "LONG" and is_bear(c):
            return {"type": "BULLISH_ORDER_BLOCK", "low": c["low"], "high": c["open"], "time": c["time"]}
        if direction == "SHORT" and is_bull(c):
            return {"type": "BEARISH_ORDER_BLOCK", "low": c["open"], "high": c["high"], "time": c["time"]}
    return None


def overlap_or_best_poi(fvg: dict | None, ob: dict | None) -> dict | None:
    if fvg and ob:
        low = max(fvg["low"], ob["low"])
        high = min(fvg["high"], ob["high"])
        if low < high:
            return {"type": "FVG_OB_OVERLAP", "low": low, "high": high}
    return fvg or ob


def build_setup(h4: list[dict], h1: list[dict], m15: list[dict], m5: list[dict], live_price: float) -> dict:
    s4 = structure(h4)
    s1 = structure(h1)
    s15 = structure(m15)
    sweep = liquidity_sweep(m15, 20)
    m5_mss = mss(m5, 8)
    disp = displacement(m5)
    if s4["bias"] in {"BULLISH", "BEARISH"}:
        bias = s4["bias"]
    elif s1["bias"] in {"BULLISH", "BEARISH"}:
        bias = s1["bias"]
    else:
        bias = "NEUTRAL"
    direction = "LONG" if bias == "BULLISH" else "SHORT" if bias == "BEARISH" else "NONE"
    valid = False
    reason = "WAIT"
    if direction == "LONG":
        valid = s1["bias"] == "BULLISH" and sweep["type"] == "SELL_SIDE_LIQUIDITY_SWEEP" and m5_mss["type"] == "BULLISH_MSS" and disp
        if valid:
            reason = "HTF bullish + sell-side sweep + 5M bullish MSS + displacement"
    elif direction == "SHORT":
        valid = s1["bias"] == "BEARISH" and sweep["type"] == "BUY_SIDE_LIQUIDITY_SWEEP" and m5_mss["type"] == "BEARISH_MSS" and disp
        if valid:
            reason = "HTF bearish + buy-side sweep + 5M bearish MSS + displacement"
    fvg = find_fvg(m5, direction) if direction != "NONE" else None
    ob = find_order_block(m5, direction) if direction != "NONE" else None
    poi = overlap_or_best_poi(fvg, ob)
    entry_low = entry_high = sl = tp1 = tp2 = None
    rr = None
    action = "WAIT"
    if valid and poi:
        entry_low, entry_high = poi["low"], poi["high"]
        mid = (entry_low + entry_high) / 2
        if direction == "LONG":
            sl = min(sweep["level"] or entry_low, entry_low) * 0.9995
            risk = mid - sl
            if risk > 0:
                tp1, tp2 = mid + risk * 2, mid + risk * 3
                rr = {"TP1": 2.0, "TP2": 3.0}
                action = "ENTER_ON_RETRACE" if entry_low <= live_price <= entry_high else "WAIT_FOR_RETRACE"
        else:
            sl = max(sweep["level"] or entry_high, entry_high) * 1.0005
            risk = sl - mid
            if risk > 0:
                tp1, tp2 = mid - risk * 2, mid - risk * 3
                rr = {"TP1": 2.0, "TP2": 3.0}
                action = "ENTER_ON_RETRACE" if entry_low <= live_price <= entry_high else "WAIT_FOR_RETRACE"
    elif valid:
        reason += "; no fresh POI found"
        action = "WAIT_FOR_POI"
    return {
        "bias": bias, "direction": direction, "setup": "VALID" if valid and poi else "WAIT", "action": action,
        "reason": reason, "live_price": live_price,
        "entry_zone": {"low": entry_low, "high": entry_high}, "stop_loss": sl,
        "take_profit_1": tp1, "take_profit_2": tp2, "risk_reward": rr,
        "4H": s4, "1H": s1, "15M": {"structure": s15, "liquidity": sweep},
        "5M": {"MSS": m5_mss, "displacement": disp, "FVG": fvg, "order_block": ob}, "POI": poi,
    }


async def _run_one(exchange: str, pair: str = DEFAULT_PAIR) -> dict:
    import asyncio
    h4, h1, m15, m5 = await asyncio.gather(fetch_klines(exchange, pair, "4h"), fetch_klines(exchange, pair, "1h"), fetch_klines(exchange, pair, "15m"), fetch_klines(exchange, pair, "5m"))
    live = await fetch_live_price(exchange, pair)
    return {"exchange": exchange, "pair": pair, "contract_type": CONTRACT_TYPE, **build_setup(h4, h1, m15, m5, live)}


async def _run_smc(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    import asyncio
    exchange = exchange.upper()
    if exchange == "BOTH":
        results = await asyncio.gather(_run_one("BINANCE", pair), _run_one("BYBIT", pair), return_exceptions=True)
        output: dict[str, Any] = {}
        for name, result in zip(("BINANCE", "BYBIT"), results):
            output[name] = {"error": str(result)} if isinstance(result, Exception) else result
        b, y = output.get("BINANCE", {}), output.get("BYBIT", {})
        if "error" not in b and "error" not in y:
            if b.get("setup") == "VALID" and y.get("setup") == "VALID" and b.get("direction") == y.get("direction"):
                consensus = "VALID_BOTH_EXCHANGES"
            elif b.get("direction") == y.get("direction") and b.get("direction") in {"LONG", "SHORT"}:
                consensus = "ALIGNED_WAIT_FOR_CONFIRMATION"
            else:
                consensus = "NO_CONSENSUS"
        else:
            consensus = "PARTIAL_DATA"
        return {"pair": pair, "contract_type": CONTRACT_TYPE, "mode": "BINANCE + BYBIT", "consensus": consensus, "BINANCE": b, "BYBIT": y}
    if exchange not in EXCHANGES:
        raise ValueError("exchange must be BINANCE, BYBIT, or BOTH")
    return await _run_one(exchange, pair)


@mcp.tool
async def get_btcusdt_perpetual_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """BTCUSDT Perpetual SMC entry setup using Binance USD-M and/or Bybit Linear."""
    return await _run_smc(exchange, pair.upper())


@mcp.tool
async def get_btc_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """Compatibility alias for the BTCUSDT perpetual SMC analysis."""
    return await _run_smc(exchange, pair.upper())


@mcp.tool
async def get_btcusdt_perpetual_market_data(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """Return normalized closed candles plus live price for 4H/1H/15M/5M."""
    async def one(ex: str) -> dict:
        import asyncio
        live, h4, h1, m15, m5 = await asyncio.gather(fetch_live_price(ex, pair), fetch_klines(ex, pair, "4h"), fetch_klines(ex, pair, "1h"), fetch_klines(ex, pair, "15m"), fetch_klines(ex, pair, "5m"))
        return {"exchange": ex, "pair": pair.upper(), "contract_type": CONTRACT_TYPE, "live_price": live, "4h": h4, "1h": h1, "15m": m15, "5m": m5}
    if exchange.upper() == "BOTH":
        import asyncio
        b, y = await asyncio.gather(one("BINANCE"), one("BYBIT"))
        return {"BINANCE": b, "BYBIT": y}
    return await one(exchange.upper())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    # Streamable HTTP exposes the standard /mcp endpoint expected by MCP clients.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
