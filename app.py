import os
from typing import Any

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

from advanced_smc import analyze_advanced

mcp = FastMCP("BTCUSDT Perpetual SMC")

DEFAULT_PAIR = "BTCUSDT"
CONTRACT_TYPE = "PERPETUAL"
VALID_INTERVALS = {"5m", "15m", "1h", "4h"}
EXCHANGES = {"BINANCE", "BYBIT"}
BINANCE_BASE_URL = os.getenv("BINANCE_FAPI_URL", "https://fapi.binance.com")
BYBIT_BASE_URL = os.getenv("BYBIT_V5_URL", "https://api.bybit.com")


def _validate_pair(pair: str) -> str:
    symbol = (pair or DEFAULT_PAIR).upper().replace("/", "")
    if symbol != DEFAULT_PAIR:
        raise ValueError("This service is restricted to BTCUSDT perpetual only")
    return DEFAULT_PAIR


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict:
    response = await client.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected response from {url}: {data}")
    return data


async def fetch_klines(exchange: str, pair: str, interval: str, limit: int = 300) -> list[dict]:
    exchange = exchange.upper()
    symbol = _validate_pair(pair)
    if exchange not in EXCHANGES:
        raise ValueError("Exchange must be BINANCE or BYBIT")
    if interval not in VALID_INTERVALS:
        raise ValueError("Interval must be one of: 5m, 15m, 1h, 4h")

    limit = min(max(int(limit), 50), 1000)
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
    symbol = _validate_pair(pair)
    if exchange not in EXCHANGES:
        raise ValueError("Exchange must be BINANCE or BYBIT")
    async with httpx.AsyncClient(timeout=10) as client:
        if exchange == "BINANCE":
            data = await _get_json(client, f"{BINANCE_BASE_URL}/fapi/v1/ticker/price", {"symbol": symbol})
            return float(data["price"])
        data = await _get_json(client, f"{BYBIT_BASE_URL}/v5/market/tickers", {"category": "linear", "symbol": symbol})
        return float(data["result"]["list"][0]["lastPrice"])


async def _run_one(exchange: str, pair: str = DEFAULT_PAIR) -> dict:
    import asyncio
    symbol = _validate_pair(pair)
    h4, h1, m15, m5 = await asyncio.gather(
        fetch_klines(exchange, symbol, "4h"), fetch_klines(exchange, symbol, "1h"),
        fetch_klines(exchange, symbol, "15m"), fetch_klines(exchange, symbol, "5m"),
    )
    live = await fetch_live_price(exchange, symbol)
    analysis = analyze_advanced(h4, h1, m15, m5, live)
    return {"exchange": exchange, "pair": symbol, "contract_type": CONTRACT_TYPE, "engine": "ADVANCED_SMC", **analysis}


def _normalize_exchange(value: str | None) -> str:
    """Normalize browser-friendly exchange values to BINANCE, BYBIT, or BOTH."""
    raw = (value or "BOTH").strip().upper()
    compact = raw.replace(" ", "")
    if compact in {"", "BOTH", "BINANCE+BYBIT", "BYBIT+BINANCE", "BINANCE,BYBIT", "BYBIT,BINANCE", "BINANCEBYBIT", "BYBITBINANCE"}:
        return "BOTH"
    return raw


async def _run_smc(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    import asyncio
    symbol = _validate_pair(pair)
    exchange = _normalize_exchange(exchange)
    if exchange == "BOTH":
        results = await asyncio.gather(_run_one("BINANCE", symbol), _run_one("BYBIT", symbol), return_exceptions=True)
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
        return {"pair": symbol, "contract_type": CONTRACT_TYPE, "mode": "BINANCE + BYBIT", "engine": "ADVANCED_SMC", "consensus": consensus, "BINANCE": b, "BYBIT": y}
    if exchange not in EXCHANGES:
        raise ValueError("exchange must be BINANCE, BYBIT, or BOTH")
    return await _run_one(exchange, symbol)


@mcp.tool
async def get_btcusdt_perpetual_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """BTCUSDT Perpetual advanced SMC setup using Binance USD-M and/or Bybit Linear."""
    return await _run_smc(exchange, pair)


@mcp.tool
async def get_btc_smc_analysis(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """Compatibility alias for the BTCUSDT perpetual advanced SMC analysis."""
    return await _run_smc(exchange, pair)


@mcp.tool
async def get_btcusdt_perpetual_market_data(exchange: str = "BOTH", pair: str = DEFAULT_PAIR) -> dict:
    """Return normalized closed BTCUSDT perpetual candles plus live price for 4H/1H/15M/5M."""
    symbol = _validate_pair(pair)

    async def one(ex: str) -> dict:
        import asyncio
        live, h4, h1, m15, m5 = await asyncio.gather(
            fetch_live_price(ex, symbol), fetch_klines(ex, symbol, "4h"), fetch_klines(ex, symbol, "1h"),
            fetch_klines(ex, symbol, "15m"), fetch_klines(ex, symbol, "5m"),
        )
        return {"exchange": ex, "pair": symbol, "contract_type": CONTRACT_TYPE, "live_price": live, "4h": h4, "1h": h1, "15m": m15, "5m": m5}

    if exchange.upper() == "BOTH":
        import asyncio
        b, y = await asyncio.gather(one("BINANCE"), one("BYBIT"))
        return {"BINANCE": b, "BYBIT": y}
    return await one(exchange.upper())


async def health(request):
    return JSONResponse({"status": "ok", "service": "Shark BTC Advanced SMC", "pair": DEFAULT_PAIR, "contract_type": CONTRACT_TYPE, "exchanges": ["BINANCE", "BYBIT"]})


async def public_smc(request):
    try:
        exchange = request.query_params.get("exchange", "BOTH")
        pair = request.query_params.get("pair", DEFAULT_PAIR)
        result = await _run_smc(exchange, pair)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


# Public read-only HTTP API. This works from a normal browser and does not require MCP.
public_app = Starlette(routes=[
    Route("/", health, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/api/smc", public_smc, methods=["GET"]),
    Mount("/mcp", app=mcp.http_app(path="/mcp", transport="streamable-http")),
])


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(public_app, host="0.0.0.0", port=port)
