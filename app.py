import os
from typing import Any

import httpx
from fastmcp import FastMCP

mcp = FastMCP("Shark BTC SMC")

BASE_URL = os.getenv(
    "SHARK_BASE_URL",
    "https://api.sharkexchange.in"
)

DEFAULT_PAIR = os.getenv(
    "SHARK_PAIR",
    "BTCUSDT"
)

VALID_INTERVALS = {"5m", "15m", "1h", "4h"}


# ---------------------------------------------------------
# SHARK MARKET DATA
# ---------------------------------------------------------

async def fetch_klines(
    pair: str,
    interval: str,
    limit: int = 200,
    price_type: str = "MARK_PRICE"
) -> list[dict]:

    if interval not in VALID_INTERVALS:
        raise ValueError(
            "Interval must be one of: 5m, 15m, 1h, 4h"
        )

    limit = min(max(int(limit), 20), 1000)

    payload = {
        "pair": pair or DEFAULT_PAIR,
        "interval": interval,
        "limit": limit
    }

    url = f"{BASE_URL}/v1/market/klines?priceType={price_type}"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"}
        )

        response.raise_for_status()
        data = response.json()

    # Shark normally returns a list.
    # Handle wrapped responses too.
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
                "volume": float(c.get("volume", 0))
            })
        except (KeyError, TypeError, ValueError):
            continue

    if len(candles) < 20:
        raise ValueError(
            f"Not enough valid candles returned for {pair} {interval}"
        )

    candles.sort(key=lambda x: x["time"])

    return candles


# ---------------------------------------------------------
# BASIC MARKET TOOLS
# ---------------------------------------------------------

@mcp.tool
async def get_btc_4h(limit: int = 200) -> dict:
    """Read Shark BTC 4-hour candles for higher-timeframe bias."""
    return {
        "pair": DEFAULT_PAIR,
        "interval": "4h",
        "candles": await fetch_klines(DEFAULT_PAIR, "4h", limit)
    }


@mcp.tool
async def get_btc_1h(limit: int = 200) -> dict:
    """Read Shark BTC 1-hour candles for BOS/CHOCH and liquidity."""
    return {
        "pair": DEFAULT_PAIR,
        "interval": "1h",
        "candles": await fetch_klines(DEFAULT_PAIR, "1h", limit)
    }


@mcp.tool
async def get_btc_15m(limit: int = 200) -> dict:
    """Read Shark BTC 15-minute candles for POI and setup context."""
    return {
        "pair": DEFAULT_PAIR,
        "interval": "15m",
        "candles": await fetch_klines(DEFAULT_PAIR, "15m", limit)
    }


@mcp.tool
async def get_btc_5m(limit: int = 200) -> dict:
    """Read Shark BTC 5-minute candles for MSS/displacement/entry."""
    return {
        "pair": DEFAULT_PAIR,
        "interval": "5m",
        "candles": await fetch_klines(DEFAULT_PAIR, "5m", limit)
    }


# ---------------------------------------------------------
# SMC HELPERS
# ---------------------------------------------------------

def body(c):
    return abs(c["close"] - c["open"])


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def recent_high(candles, lookback=20):
    data = candles[-lookback:]
    return max(c["high"] for c in data)


def recent_low(candles, lookback=20):
    data = candles[-lookback:]
    return min(c["low"] for c in data)


def detect_structure(candles, lookback=20):
    """
    Simple structure model:
    - close above previous range high => bullish BOS
    - close below previous range low => bearish BOS
    """

    if len(candles) < lookback + 2:
        return {
            "structure": "UNKNOWN",
            "bos": False
        }

    current = candles[-1]
    previous = candles[-lookback-1:-1]

    previous_high = max(c["high"] for c in previous)
    previous_low = min(c["low"] for c in previous)

    if current["close"] > previous_high:
        return {
            "structure": "BULLISH",
            "bos": True,
            "level": previous_high
        }

    if current["close"] < previous_low:
        return {
            "structure": "BEARISH",
            "bos": True,
            "level": previous_low
        }

    return {
        "structure": "RANGE",
        "bos": False,
        "high": previous_high,
        "low": previous_low
    }


def detect_fvg(candles):
    """
    Three-candle fair value gap.
    Bullish FVG:
        candle1.high < candle3.low
    Bearish FVG:
        candle1.low > candle3.high
    """

    if len(candles) < 3:
        return None

    a = candles[-3]
    b = candles[-2]
    c = candles[-1]

    if a["high"] < c["low"]:
        return {
            "type": "BULLISH",
            "low": a["high"],
            "high": c["low"]
        }

    if a["low"] > c["high"]:
        return {
            "type": "BEARISH",
            "low": c["high"],
            "high": a["low"]
        }

    return None


def detect_mss(candles, lookback=8):
    """
    Simple short-term market structure shift.
    """

    if len(candles) < lookback + 3:
        return "NONE"

    recent = candles[-lookback-1:-1]
    current = candles[-1]

    high = max(c["high"] for c in recent)
    low = min(c["low"] for c in recent)

    if current["close"] > high:
        return "BULLISH_MSS"

    if current["close"] < low:
        return "BEARISH_MSS"

    return "NONE"


def detect_displacement(candles):
    if len(candles) < 10:
        return False

    current = candles[-1]

    recent_bodies = [
        body(c) for c in candles[-10:-1]
    ]

    average_body = sum(recent_bodies) / len(recent_bodies)

    return body(current) >= average_body * 1.5


# ---------------------------------------------------------
# FULL SMC ANALYSIS
# ---------------------------------------------------------

@mcp.tool
async def get_btc_smc_analysis() -> dict:
    """
    Full BTC multi-timeframe SMC analysis:
    4H bias -> 1H structure -> 15M POI/FVG -> 5M MSS.
    """

    h4 = await fetch_klines(DEFAULT_PAIR, "4h", 200)
    h1 = await fetch_klines(DEFAULT_PAIR, "1h", 200)
    m15 = await fetch_klines(DEFAULT_PAIR, "15m", 200)
    m5 = await fetch_klines(DEFAULT_PAIR, "5m", 200)

    h4_structure = detect_structure(h4)
    h1_structure = detect_structure(h1)
    m15_structure = detect_structure(m15)

    m15_fvg = detect_fvg(m15)
    m5_fvg = detect_fvg(m5)

    m5_mss = detect_mss(m5)
    displacement = detect_displacement(m5)

    price = m5[-1]["close"]

    # -----------------------------------------------------
    # HIGHER TIMEFRAME BIAS
    # -----------------------------------------------------

    if h4_structure["structure"] == "BULLISH":
        bias = "BULLISH"
    elif h4_structure["structure"] == "BEARISH":
        bias = "BEARISH"
    else:
        # fallback to 1H
        if h1_structure["structure"] == "BULLISH":
            bias = "BULLISH"
        elif h1_structure["structure"] == "BEARISH":
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

    # -----------------------------------------------------
    # SETUP VALIDATION
    # -----------------------------------------------------

    setup = "WAIT"
    direction = "NONE"

    if bias == "BULLISH":
        direction = "LONG"

        if (
            h1_structure["structure"] == "BULLISH"
            and m5_mss == "BULLISH_MSS"
            and displacement
        ):
            setup = "VALID_LONG"

    elif bias == "BEARISH":
        direction = "SHORT"

        if (
            h1_structure["structure"] == "BEARISH"
            and m5_mss == "BEARISH_MSS"
            and displacement
        ):
            setup = "VALID_SHORT"

    # -----------------------------------------------------
    # ENTRY / SL / TP
    # -----------------------------------------------------

    entry = price
    sl = None
    tp1 = None
    tp2 = None
    rr = None

    if setup == "VALID_LONG":

        swing_low = recent_low(m5, 20)

        sl = swing_low

        risk = entry - sl

        if risk > 0:
            tp1 = entry + risk * 2
            tp2 = entry + risk * 3

            rr = {
                "TP1": 2.0,
                "TP2": 3.0
            }

    elif setup == "VALID_SHORT":

        swing_high = recent_high(m5, 20)

        sl = swing_high

        risk = sl - entry

        if risk > 0:
            tp1 = entry - risk * 2
            tp2 = entry - risk * 3

            rr = {
                "TP1": 2.0,
                "TP2": 3.0
            }

    # -----------------------------------------------------
    # POI
    # -----------------------------------------------------

    poi = None

    if m15_fvg:
        poi = m15_fvg

    elif m5_fvg:
        poi = m5_fvg

    # -----------------------------------------------------
    # FINAL RESULT
    # -----------------------------------------------------

    return {
        "pair": DEFAULT_PAIR,

        "current_price": price,

        "bias": bias,
        "direction": direction,
        "setup": setup,

        "4H": {
            "structure": h4_structure
        },

        "1H": {
            "structure": h1_structure
        },

        "15M": {
            "structure": m15_structure,
            "FVG": m15_fvg
        },

        "5M": {
            "MSS": m5_mss,
            "displacement": displacement,
            "FVG": m5_fvg
        },

        "POI": poi,

        "entry": entry,
        "stop_loss": sl,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_reward": rr,

        "trade_action": (
            "WAIT FOR CONFIRMATION"
            if setup == "WAIT"
            else "SMC SETUP DETECTED - MANUAL REVIEW REQUIRED"
        )
    }


# ---------------------------------------------------------
# RAW MULTI-TIMEFRAME DATA
# ---------------------------------------------------------

@mcp.tool
async def get_btc_smc_data() -> dict:
    """Return 4H, 1H, 15M and 5M Shark candles."""

    return {
        "pair": DEFAULT_PAIR,
        "4h": await fetch_klines(DEFAULT_PAIR, "4h", 200),
        "1h": await fetch_klines(DEFAULT_PAIR, "1h", 200),
        "15m": await fetch_klines(DEFAULT_PAIR, "15m", 200),
        "5m": await fetch_klines(DEFAULT_PAIR, "5m", 200)
    }


# ---------------------------------------------------------
# SERVER
# ---------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port
            )
