import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP("Shark BTC SMC")

BASE_URL = os.getenv("SHARK_BASE_URL", "https://api.sharkexchange.in")
DEFAULT_PAIR = os.getenv("SHARK_PAIR", "BTCUSDT")

async def fetch_klines(pair: str, interval: str, limit: int = 200, price_type: str = "MARK_PRICE"):
    if interval not in {"5m", "15m", "1h", "4h"}:
        raise ValueError("interval must be one of: 5m, 15m, 1h, 4h")
    payload = {
        "pair": pair or DEFAULT_PAIR,
        "interval": interval,
        "limit": min(max(int(limit), 1), 1000),
        "priceType": price_type,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{BASE_URL}/v1/market/klines", json=payload)
        r.raise_for_status()
        return r.json()

@mcp.tool
async def get_btc_4h(limit: int = 200) -> dict:
    """Read Shark BTC 4-hour candles for SMC higher-timeframe bias."""
    return await fetch_klines(DEFAULT_PAIR, "4h", limit)

@mcp.tool
async def get_btc_1h(limit: int = 200) -> dict:
    """Read Shark BTC 1-hour candles for BOS/CHOCH and liquidity."""
    return await fetch_klines(DEFAULT_PAIR, "1h", limit)

@mcp.tool
async def get_btc_15m(limit: int = 200) -> dict:
    """Read Shark BTC 15-minute candles for POI and setup context."""
    return await fetch_klines(DEFAULT_PAIR, "15m", limit)

@mcp.tool
async def get_btc_5m(limit: int = 200) -> dict:
    """Read Shark BTC 5-minute candles for MSS, displacement and FVG."""
    return await fetch_klines(DEFAULT_PAIR, "5m", limit)

@mcp.tool
async def get_btc_smc_data() -> dict:
    """Fetch 4H, 1H, 15M and 5M Shark candles for SMC analysis."""
    return {
        "pair": DEFAULT_PAIR,
        "4h": await fetch_klines(DEFAULT_PAIR, "4h", 200),
        "1h": await fetch_klines(DEFAULT_PAIR, "1h", 200),
        "15m": await fetch_klines(DEFAULT_PAIR, "15m", 200),
        "5m": await fetch_klines(DEFAULT_PAIR, "5m", 200),
    }

if __name__ == "__main__":
    # FastMCP exposes a remote Streamable HTTP MCP server.
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
