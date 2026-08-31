import os
from typing import Any

import httpx
from fastmcp import FastMCP

mcp = FastMCP("Shark BTC SMC")

# Keep the existing Shark exchange/data source unchanged.
BASE_URL = os.getenv("SHARK_BASE_URL", "https://api.sharkexchange.in")
DEFAULT_PAIR = os.getenv("SHARK_PAIR", "BTCUSDT")
CONTRACT_TYPE = "PERPETUAL"
VALID_INTERVALS = {"5m", "15m", "1h", "4h"}


# ---------------------------------------------------------
# SHARK MARKET DATA — BTCUSDT PERPETUAL
# ---------------------------------------------------------

async def fetch_klines(
    pair: str,
    interval: str,
    limit: int = 200,
    price_type: str = "MARK_PRICE",
) -> list[dict]:
    """Fetch existing Shark BTCUSDT perpetual candles without changing exchange."""
    if interval not in VALID_INTERVALS:
        raise ValueError("Interval must be one of: 5m, 15m, 1h, 4h")

    limit = min(max(int(limit), 20), 1000)
    payload = {
        "pair": pair or DEFAULT_PAIR,
        "interval": interval,
        "limit": limit,
    }

    url = f"{BASE_URL}/v1/market/klines?priceType={price_type}"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

    if isinstance(data, dict):
        for key in ("data", "result", "rows", "klines"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if not isinstance(data, list):
        raise ValueError(f"Unexpected Shark kline response: {data}")

    candles = []
    for c in data:
        if not isinstance(c, dict):
            continue
        try:
            candles.append({
                "time": int(c["startTime"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "endTime": int(c["endTime"]),
                "volume": float(c.get("volume", 0)),
            })
        except (KeyError, TypeError, ValueError):
            continue

    if len(candles) < 20:
        raise ValueError(f"Not enough valid candles returned for {pair} {interval}")

    candles.sort(key=lambda x: x["time"])
    return candles


async def _perpetual_candles(interval: str, limit: int = 200) -> list[dict]:
    """Canonical BTCUSDT perpetual data path; exchange remains Shark."""
    return await fetch_klines(DEFAULT_PAIR, interval, limit, "MARK_PRICE")


# ---------------------------------------------------------
# BASIC MARKET TOOLS — compatibility preserved
# ---------------------------------------------------------

@mcp.tool
async def get_btc_4h(limit: int = 200) -> dict:
    return {"pair": DEFAULT_PAIR, "contract_type": CONTRACT_TYPE, "interval": "4h", "candles": await _perpetual_candles("4h", limit)}


@mcp.tool
async def get_btc_1h(limit: int = 200) -> dict:
    return {"pair": DEFAULT_PAIR, "contract_type": CONTRACT_TYPE, "interval": "1h", "candles": await _perpetual_candles("1h", limit)}


@mcp.tool
async def get_btc_15m(limit: int = 200) -> dict:
    return {"pair": DEFAULT_PAIR, "contract_type": CONTRACT_TYPE, "interval": "15m", "candles": await _perpetual_candles("15m", limit)}


@mcp.tool
async def get_btc_5m(limit: int = 200) -> dict:
    return {"pair": DEFAULT_PAIR, "contract_type": CONTRACT_TYPE, "interval": "5m", "candles": await _perpetual_candles("5m", limit)}


# ---------------------------------------------------------
# SMC HELPERS
# ---------------------------------------------------------

def body(c: dict) -> float:
    return abs(c["close"] - c["open"])


def bullish(c: dict) -> bool:
    return c["close"] > c["open"]


def bearish(c: dict) -> bool:
    return c["close"] < c["open"]


def recent_high(candles: list[dict], lookback: int = 20) -> float:
    return max(c["high"] for c in candles[-lookback:])


def recent_low(candles: list[dict], lookback: int = 20) -> float:
    return min(c["low"] for c in candles[-lookback:])


def detect_structure(candles: list[dict], lookback: int = 20) -> dict:
    if len(candles) < lookback + 2:
        return {"structure": "UNKNOWN", "bos": False}

    current = candles[-1]
    previous = candles[-lookback - 1:-1]
    previous_high = max(c["high"] for c in previous)
    previous_low = min(c["low"] for c in previous)

    if current["close"] > previous_high:
        return {"structure": "BULLISH", "bos": True, "level": previous_high}
    if current["close"] < previous_low:
        return {"structure": "BEARISH", "bos": True, "level": previous_low}
    return {"structure": "RANGE", "bos": False, "high": previous_high, "low": previous_low}


def detect_fvg(candles: list[dict]) -> dict | None:
    if len(candles) < 3:
        return None
    a, _, c = candles[-3], candles[-2], candles[-1]
    if a["high"] < c["low"]:
        return {"type": "BULLISH", "low": a["high"], "high": c["low"]}
    if a["low"] > c["high"]:
        return {"type": "BEARISH", "low": c["high"], "high": a["low"]}
    return None


def detect_mss(candles: list[dict], lookback: int = 8) -> str:
    if len(candles) < lookback + 3:
        return "NONE"
    recent = candles[-lookback - 1:-1]
    current = candles[-1]
    high = max(c["high"] for c in recent)
    low = min(c["low"] for c in recent)
    if current["close"] > high:
        return "BULLISH_MSS"
    if current["close"] < low:
        return "BEARISH_MSS"
    return "NONE"


def detect_displacement(candles: list[dict]) -> bool:
    if len(candles) < 10:
        return False
    recent_bodies = [body(c) for c in candles[-10:-1]]
    average_body = sum(recent_bodies) / len(recent_bodies)
    return body(candles[-1]) >= average_body * 1.5


def detect_liquidity_sweep(candles: list[dict], lookback: int = 10) -> str:
    """Detect a simple wick sweep of recent liquidity."""
    if len(candles) < lookback + 1:
        return "NONE"
    current = candles[-1]
    prior = candles[-lookback - 1:-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)

    if current["high"] > prior_high and current["close"] < prior_high:
        return "BUY_SIDE_SWEEP"
    if current["low"] < prior_low and current["close"] > prior_low:
        return "SELL_SIDE_SWEEP"
    return "NONE"


# ---------------------------------------------------------
# FULL SMC ANALYSIS — BTCUSDT PERPETUAL
# ---------------------------------------------------------

async def _run_smc() -> dict:
    h4 = await _perpetual_candles("4h", 200)
    h1 = await _perpetual_candles("1h", 200)
    m15 = await _perpetual_candles("15m", 200)
    m5 = await _perpetual_candles("5m", 200)

    h4_structure = detect_structure(h4)
    h1_structure = detect_structure(h1)
    m15_structure = detect_structure(m15)
    m15_fvg = detect_fvg(m15)
    m5_fvg = detect_fvg(m5)
    m5_mss = detect_mss(m5)
    displacement = detect_displacement(m5)
    sweep = detect_liquidity_sweep(m5)
    price = m5[-1]["close"]

    if h4_structure["structure"] in {"BULLISH", "BEARISH"}:
        bias = h4_structure["structure"]
    elif h1_structure["structure"] in {"BULLISH", "BEARISH"}:
        bias = h1_structure["structure"]
    else:
        bias = "NEUTRAL"

    setup = "WAIT"
    direction = "NONE"

    # Conservative SMC chain: HTF alignment + liquidity sweep + MSS + displacement.
    if bias == "BULLISH":
        direction = "LONG"
        if (
            h1_structure["structure"] == "BULLISH"
            and sweep == "SELL_SIDE_SWEEP"
            and m5_mss == "BULLISH_MSS"
            and displacement
        ):
            setup = "VALID_LONG"
    elif bias == "BEARISH":
        direction = "SHORT"
        if (
            h1_structure["structure"] == "BEARISH"
            and sweep == "BUY_SIDE_SWEEP"
            and m5_mss == "BEARISH_MSS"
            and displacement
        ):
            setup = "VALID_SHORT"

    entry = price
    sl = tp1 = tp2 = None
    rr = None

    if setup == "VALID_LONG":
        sl = recent_low(m5, 20)
        risk = entry - sl
        if risk > 0:
            tp1 = entry + risk * 2
            tp2 = entry + risk * 3
            rr = {"TP1": 2.0, "TP2": 3.0}
    elif setup == "VALID_SHORT":
        sl = recent_high(m5, 20)
        risk = sl - entry
        if risk > 0:
            tp1 = entry - risk * 2
            tp2 = entry - risk * 3
            rr = {"TP1": 2.0, "TP2": 3.0}

    poi = m15_fvg or m5_fvg

    return {
        "exchange": "SHARK",
        "pair": DEFAULT_PAIR,
        "contract_type": CONTRACT_TYPE,
        "price_type": "MARK_PRICE",
        "current_price": price,
        "bias": bias,
        "direction": direction,
        "setup": setup,
        "4H": {"structure": h4_structure},
        "1H": {"structure": h1_structure},
        "15M": {"structure": m15_structure, "FVG": m15_fvg},
        "5M": {"MSS": m5_mss, "displacement": displacement, "liquidity_sweep": sweep, "FVG": m5_fvg},
        "POI": poi,
        "entry": entry,
        "stop_loss": sl,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_reward": rr,
        "trade_action": "WAIT FOR CONFIRMATION" if setup == "WAIT" else "SMC SETUP DETECTED - MANUAL REVIEW REQUIRED",
    }


@mcp.tool
async def get_btc_smc_analysis() -> dict:
    """BTCUSDT perpetual SMC: 4H -> 1H -> 15M -> 5M."""
    return await _run_smc()


@mcp.tool
async def get_btcusdt_perpetual_smc_analysis() -> dict:
    """Explicit BTCUSDT Perpetual SMC tool; uses the existing Shark exchange unchanged."""
    return await _run_smc()


@mcp.tool
async def get_btc_smc_data() -> dict:
    """Return existing Shark BTCUSDT perpetual 4H/1H/15M/5M candles."""
    return {
        "exchange": "SHARK",
        "pair": DEFAULT_PAIR,
        "contract_type": CONTRACT_TYPE,
        "price_type": "MARK_PRICE",
        "4h": await _perpetual_candles("4h", 200),
        "1h": await _perpetual_candles("1h", 200),
        "15m": await _perpetual_candles("15m", 200),
        "5m": await _perpetual_candles("5m", 200),
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
