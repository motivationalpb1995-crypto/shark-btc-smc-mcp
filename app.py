import asyncio
import os
from typing import Any

import httpx
from fastmcp import FastMCP

mcp = FastMCP("BTC SMC Futures")

DEFAULT_PAIR = os.getenv("DEFAULT_PAIR", "BTCUSDT")
BINANCE_SPOT_URL = os.getenv("BINANCE_SPOT_URL", "https://api.binance.com")
BINANCE_FUTURES_URL = os.getenv("BINANCE_FUTURES_URL", "https://fapi.binance.com")
BYBIT_URL = os.getenv("BYBIT_URL", "https://api.bybit.com")
VALID_INTERVALS = {"5m", "15m", "1h", "4h"}


async def _get(url: str, params: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def _normalize_candle(c: list[Any]) -> dict:
    return {
        "time": int(c[0]),
        "open": float(c[1]),
        "high": float(c[2]),
        "low": float(c[3]),
        "close": float(c[4]),
        "volume": float(c[5]),
        "endTime": int(c[6]) if len(c) > 6 else None,
    }


async def fetch_binance_klines(pair: str, interval: str, limit: int = 200) -> list[dict]:
    if interval not in VALID_INTERVALS:
        raise ValueError("Interval must be one of: 5m, 15m, 1h, 4h")
    limit = min(max(int(limit), 20), 1500)
    data = await _get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/klines",
        {"symbol": (pair or DEFAULT_PAIR).upper(), "interval": interval, "limit": limit},
    )
    if not isinstance(data, list):
        raise ValueError(f"Unexpected Binance Futures response: {data}")
    return sorted([_normalize_candle(c) for c in data if isinstance(c, list) and len(c) >= 6], key=lambda x: x["time"])


async def fetch_bybit_klines(pair: str, interval: str, limit: int = 200) -> list[dict]:
    if interval not in VALID_INTERVALS:
        raise ValueError("Interval must be one of: 5m, 15m, 1h, 4h")
    limit = min(max(int(limit), 20), 1000)
    interval_map = {"5m": "5", "15m": "15", "1h": "60", "4h": "240"}
    data = await _get(
        f"{BYBIT_URL}/v5/market/kline",
        {
            "category": "linear",
            "symbol": (pair or DEFAULT_PAIR).upper(),
            "interval": interval_map[interval],
            "limit": limit,
        },
    )
    if not isinstance(data, dict) or data.get("retCode") != 0:
        raise ValueError(f"Unexpected Bybit response: {data}")
    rows = data.get("result", {}).get("list", [])
    candles = []
    for c in rows:
        if not isinstance(c, list) or len(c) < 6:
            continue
        candles.append({
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
            "endTime": None,
        })
    candles.sort(key=lambda x: x["time"])
    return candles


async def fetch_klines(exchange: str, pair: str, interval: str, limit: int = 200) -> list[dict]:
    exchange = exchange.lower()
    if exchange == "binance":
        candles = await fetch_binance_klines(pair, interval, limit)
    elif exchange == "bybit":
        candles = await fetch_bybit_klines(pair, interval, limit)
    else:
        raise ValueError("Exchange must be 'binance' or 'bybit'")
    if len(candles) < 20:
        raise ValueError(f"Not enough valid candles returned for {exchange} {pair} {interval}")
    return candles


async def fetch_binance_futures_snapshot(pair: str) -> dict:
    symbol = (pair or DEFAULT_PAIR).upper()
    ticker, mark, book, oi = await asyncio.gather(
        _get(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr", {"symbol": symbol}),
        _get(f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex", {"symbol": symbol}),
        _get(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/bookTicker", {"symbol": symbol}),
        _get(f"{BINANCE_FUTURES_URL}/fapi/v1/openInterest", {"symbol": symbol}),
    )
    depth = await _get(f"{BINANCE_FUTURES_URL}/fapi/v1/depth", {"symbol": symbol, "limit": 20})
    return {
        "exchange": "binance",
        "market_type": "USDT-M perpetual futures",
        "symbol": symbol,
        "last_price": float(ticker["lastPrice"]),
        "price_change_24h_pct": float(ticker["priceChangePercent"]),
        "mark_price": float(mark["markPrice"]),
        "index_price": float(mark["indexPrice"]),
        "funding_rate": float(mark["lastFundingRate"]),
        "next_funding_time": int(mark["nextFundingTime"]),
        "best_ask": float(book["askPrice"]),
        "best_ask_qty": float(book["askQty"]),
        "best_bid": float(book["bidPrice"]),
        "best_bid_qty": float(book["bidQty"]),
        "open_interest": float(oi["openInterest"]),
        "order_book": {
            "bids": [[float(x[0]), float(x[1])] for x in depth.get("bids", [])],
            "asks": [[float(x[0]), float(x[1])] for x in depth.get("asks", [])],
        },
    }


async def fetch_bybit_futures_snapshot(pair: str) -> dict:
    symbol = (pair or DEFAULT_PAIR).upper()
    ticker = await _get(f"{BYBIT_URL}/v5/market/tickers", {"category": "linear", "symbol": symbol})
    if ticker.get("retCode") != 0 or not ticker.get("result", {}).get("list"):
        raise ValueError(f"Unexpected Bybit ticker response: {ticker}")
    t = ticker["result"]["list"][0]
    orderbook, oi = await asyncio.gather(
        _get(f"{BYBIT_URL}/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": 20}),
        _get(f"{BYBIT_URL}/v5/market/open-interest", {"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": 1}),
    )
    if orderbook.get("retCode") != 0:
        raise ValueError(f"Unexpected Bybit order book response: {orderbook}")
    oi_rows = oi.get("result", {}).get("list", [])
    return {
        "exchange": "bybit",
        "market_type": "USDT perpetual futures",
        "symbol": symbol,
        "last_price": float(t["lastPrice"]),
        "price_change_24h_pct": float(t["price24hPcnt"]) * 100,
        "mark_price": float(t["markPrice"]),
        "index_price": float(t["indexPrice"]),
        "funding_rate": float(t["fundingRate"]),
        "next_funding_time": int(t["nextFundingTime"]),
        "best_ask": float(t["ask1Price"]),
        "best_ask_qty": float(t["ask1Size"]),
        "best_bid": float(t["bid1Price"]),
        "best_bid_qty": float(t["bid1Size"]),
        "open_interest": float(oi_rows[0]["openInterest"]) if oi_rows else None,
        "order_book": {
            "bids": [[float(x[0]), float(x[1])] for x in orderbook.get("result", {}).get("b", [])],
            "asks": [[float(x[0]), float(x[1])] for x in orderbook.get("result", {}).get("a", [])],
        },
    }


async def fetch_futures_snapshot(exchange: str, pair: str = DEFAULT_PAIR) -> dict:
    exchange = exchange.lower()
    if exchange == "binance":
        return await fetch_binance_futures_snapshot(pair)
    if exchange == "bybit":
        return await fetch_bybit_futures_snapshot(pair)
    raise ValueError("Exchange must be 'binance' or 'bybit'")


@mcp.tool
async def get_binance_btc_futures_snapshot() -> dict:
    """BTCUSDT USDT-M perpetual snapshot: last/mark/index price, funding, bid/ask, OI and order book."""
    return await fetch_futures_snapshot("binance")


@mcp.tool
async def get_bybit_btc_futures_snapshot() -> dict:
    """BTCUSDT linear perpetual snapshot: last/mark/index price, funding, bid/ask, OI and order book."""
    return await fetch_futures_snapshot("bybit")


@mcp.tool
async def get_btc_futures_comparison() -> dict:
    """Compare the same BTCUSDT perpetual contract data across Binance and Bybit."""
    binance, bybit = await asyncio.gather(
        fetch_futures_snapshot("binance"),
        fetch_futures_snapshot("bybit"),
    )
    return {"symbol": DEFAULT_PAIR, "binance": binance, "bybit": bybit}


@mcp.tool
async def get_binance_btc_4h(limit: int = 200) -> dict:
    return {"exchange": "binance", "market_type": "USDT-M perpetual futures", "pair": DEFAULT_PAIR, "interval": "4h", "candles": await fetch_klines("binance", DEFAULT_PAIR, "4h", limit)}


@mcp.tool
async def get_binance_btc_1h(limit: int = 200) -> dict:
    return {"exchange": "binance", "market_type": "USDT-M perpetual futures", "pair": DEFAULT_PAIR, "interval": "1h", "candles": await fetch_klines("binance", DEFAULT_PAIR, "1h", limit)}


@mcp.tool
async def get_binance_btc_15m(limit: int = 200) -> dict:
    return {"exchange": "binance", "market_type": "USDT-M perpetual futures", "pair": DEFAULT_PAIR, "interval": "15m", "candles": await fetch_klines("binance", DEFAULT_PAIR, "15m", limit)}


@mcp.tool
async def get_binance_btc_5m(limit: int = 200) -> dict:
    return {"exchange": "binance", "market_type": "USDT-M perpetual futures", "pair": DEFAULT_PAIR, "interval": "5m", "candles": await fetch_klines("binance", DEFAULT_PAIR, "5m", limit)}


@mcp.tool
async def get_bybit_btc_4h(limit: int = 200) -> dict:
    return {"exchange": "bybit", "market_type": "USDT perpetual futures", "pair": DEFAULT_PAIR, "interval": "4h", "candles": await fetch_klines("bybit", DEFAULT_PAIR, "4h", limit)}


@mcp.tool
async def get_bybit_btc_1h(limit: int = 200) -> dict:
    return {"exchange": "bybit", "market_type": "USDT perpetual futures", "pair": DEFAULT_PAIR, "interval": "1h", "candles": await fetch_klines("bybit", DEFAULT_PAIR, "1h", limit)}


@mcp.tool
async def get_bybit_btc_15m(limit: int = 200) -> dict:
    return {"exchange": "bybit", "market_type": "USDT perpetual futures", "pair": DEFAULT_PAIR, "interval": "15m", "candles": await fetch_klines("bybit", DEFAULT_PAIR, "15m", limit)}


@mcp.tool
async def get_bybit_btc_5m(limit: int = 200) -> dict:
    return {"exchange": "bybit", "market_type": "USDT perpetual futures", "pair": DEFAULT_PAIR, "interval": "5m", "candles": await fetch_klines("bybit", DEFAULT_PAIR, "5m", limit)}


def body(c: dict) -> float:
    return abs(c["close"] - c["open"])


def recent_high(candles: list[dict], lookback: int = 20) -> float:
    return max(c["high"] for c in candles[-lookback:])


def recent_low(candles: list[dict], lookback: int = 20) -> float:
    return min(c["low"] for c in candles[-lookback:])


def detect_structure(candles: list[dict], lookback: int = 20) -> dict:
    if len(candles) < lookback + 2:
        return {"structure": "UNKNOWN", "bos": False}
    current = candles[-1]
    previous = candles[-lookback-1:-1]
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
    recent = candles[-lookback-1:-1]
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
    average_body = sum(body(c) for c in candles[-10:-1]) / 9
    return body(candles[-1]) >= average_body * 1.5


def liquidity_sweep(candles: list[dict], lookback: int = 20) -> dict:
    if len(candles) < lookback + 2:
        return {"type": "NONE"}
    prior = candles[-lookback-1:-1]
    current = candles[-1]
    prior_high = max(c["high"] for c in prior)
    prior_low = min(c["low"] for c in prior)
    if current["high"] > prior_high and current["close"] < prior_high:
        return {"type": "BUY_SIDE_SWEEP", "level": prior_high}
    if current["low"] < prior_low and current["close"] > prior_low:
        return {"type": "SELL_SIDE_SWEEP", "level": prior_low}
    return {"type": "NONE", "buy_side": prior_high, "sell_side": prior_low}


async def analyze_exchange(exchange: str) -> dict:
    h4, h1, m15, m5, snapshot = await asyncio.gather(
        fetch_klines(exchange, DEFAULT_PAIR, "4h", 200),
        fetch_klines(exchange, DEFAULT_PAIR, "1h", 200),
        fetch_klines(exchange, DEFAULT_PAIR, "15m", 200),
        fetch_klines(exchange, DEFAULT_PAIR, "5m", 200),
        fetch_futures_snapshot(exchange, DEFAULT_PAIR),
    )
    h4s, h1s, m15s = detect_structure(h4), detect_structure(h1), detect_structure(m15)
    m15_fvg, m5_fvg = detect_fvg(m15), detect_fvg(m5)
    m5_mss, displacement = detect_mss(m5), detect_displacement(m5)
    sweep = liquidity_sweep(m5)
    price = snapshot["last_price"]
    bias = h4s["structure"] if h4s["structure"] in {"BULLISH", "BEARISH"} else h1s["structure"] if h1s["structure"] in {"BULLISH", "BEARISH"} else "NEUTRAL"
    setup, direction = "WAIT", "NONE"
    if bias == "BULLISH":
        direction = "LONG"
        if sweep["type"] == "SELL_SIDE_SWEEP" and m5_mss == "BULLISH_MSS" and displacement:
            setup = "VALID_LONG"
    elif bias == "BEARISH":
        direction = "SHORT"
        if sweep["type"] == "BUY_SIDE_SWEEP" and m5_mss == "BEARISH_MSS" and displacement:
            setup = "VALID_SHORT"
    entry = price
    sl = tp1 = tp2 = None
    rr = None
    if setup == "VALID_LONG":
        sl = recent_low(m5, 20)
        risk = entry - sl
        if risk > 0:
            tp1, tp2, rr = entry + risk * 2, entry + risk * 3, {"TP1": 2.0, "TP2": 3.0}
    elif setup == "VALID_SHORT":
        sl = recent_high(m5, 20)
        risk = sl - entry
        if risk > 0:
            tp1, tp2, rr = entry - risk * 2, entry - risk * 3, {"TP1": 2.0, "TP2": 3.0}
    return {
        "exchange": exchange,
        "market_type": snapshot["market_type"],
        "pair": DEFAULT_PAIR,
        "market": snapshot,
        "current_price": price,
        "bias": bias,
        "direction": direction,
        "setup": setup,
        "4H": {"structure": h4s},
        "1H": {"structure": h1s},
        "15M": {"structure": m15s, "FVG": m15_fvg},
        "5M": {"MSS": m5_mss, "displacement": displacement, "liquidity_sweep": sweep, "FVG": m5_fvg},
        "POI": m15_fvg or m5_fvg,
        "entry": entry,
        "stop_loss": sl,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_reward": rr,
        "trade_action": "WAIT FOR LIQUIDITY + MSS + DISPLACEMENT + RETEST" if setup == "WAIT" else "SMC SETUP DETECTED - MANUAL REVIEW REQUIRED",
    }


@mcp.tool
async def get_btc_smc_analysis(exchange: str = "binance") -> dict:
    """Multi-timeframe BTCUSDT perpetual SMC analysis using Binance or Bybit futures data."""
    if exchange.lower() not in {"binance", "bybit"}:
        raise ValueError("Exchange must be 'binance' or 'bybit'")
    return await analyze_exchange(exchange.lower())


@mcp.tool
async def get_btc_smc_comparison() -> dict:
    """Compare BTCUSDT perpetual SMC state across Binance and Bybit."""
    binance, bybit = await asyncio.gather(analyze_exchange("binance"), analyze_exchange("bybit"))
    agreement = binance["bias"] == bybit["bias"] and binance["direction"] == bybit["direction"]
    return {"pair": DEFAULT_PAIR, "market_type": "USDT perpetual futures", "binance": binance, "bybit": bybit, "exchange_agreement": agreement}


@mcp.tool
async def get_btc_market_data(exchange: str = "binance") -> dict:
    """Return BTCUSDT perpetual futures snapshot and 4H/1H/15M/5M candles."""
    exchange = exchange.lower()
    if exchange not in {"binance", "bybit"}:
        raise ValueError("Exchange must be 'binance' or 'bybit'")
    h4, h1, m15, m5, snapshot = await asyncio.gather(
        fetch_klines(exchange, DEFAULT_PAIR, "4h", 200),
        fetch_klines(exchange, DEFAULT_PAIR, "1h", 200),
        fetch_klines(exchange, DEFAULT_PAIR, "15m", 200),
        fetch_klines(exchange, DEFAULT_PAIR, "5m", 200),
        fetch_futures_snapshot(exchange, DEFAULT_PAIR),
    )
    return {"exchange": exchange, "market_type": snapshot["market_type"], "pair": DEFAULT_PAIR, "snapshot": snapshot, "4h": h4, "1h": h1, "15m": m15, "5m": m5}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
