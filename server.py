import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from app import mcp


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse(
        {
            "service": "shark-btc-smc-mcp",
            "status": "ok",
            "message": "Shark BTCUSDT Perpetual SMC server is live",
            "mcp_endpoint": "/mcp",
            "health_endpoint": "/health",
        }
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "healthy", "service": "shark-btc-smc-mcp"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
