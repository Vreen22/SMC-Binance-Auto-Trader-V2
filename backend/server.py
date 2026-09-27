import os, time, hmac, hashlib
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv("BINANCE_API_KEY", "")
SECRET = os.getenv("BINANCE_API_SECRET", "")

# Binance Futures Demo Trading
BASE = os.getenv(
    "BINANCE_BASE_URL",
    "https://demo-fapi.binance.com"
).rstrip("/")

MAX = float(os.getenv("MAX_USDT_PER_TRADE", "20"))
LEV = int(os.getenv("LEVERAGE", "1"))

app = FastAPI(title="SMC Binance Auto Trader V2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

state = {
    "auto": False,
    "emergency": False,
    "active": None,
    "last": None,
}


class Auto(BaseModel):
    enabled: bool


class Signal(BaseModel):
    symbol: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    quantity_usdt: float = Field(gt=0)
    candle_time: int


def signed(params):
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000

    query = urlencode(p)

    p["signature"] = hmac.new(
        SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

    return p


async def req(method, path, params=None):
    if not KEY or not SECRET:
        raise HTTPException(
            500,
            "API credentials missing"
        )

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.request(
            method,
            BASE + path,
            params=signed(params or {}),
            headers={
                "X-MBX-APIKEY": KEY
            },
        )

    if r.status_code >= 400:
        raise HTTPException(
            r.status_code,
            r.text
        )

    return r.json()


async def public_get(path, params=None):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            BASE + path,
            params=params or {}
        )

    if r.status_code >= 400:
        raise HTTPException(
            r.status_code,
            r.text
        )

    return r.json()


async def info(sym):
    data = await public_get(
        "/fapi/v1/exchangeInfo"
    )

    for s in data["symbols"]:
        if s["symbol"] == sym:
            return s

    raise HTTPException(
        400,
        "Unknown Futures symbol"
    )


def floorq(q, step):
    a = Decimal(str(q))
    b = Decimal(str(step))

    return float(
        (a / b).to_integral_value(
            rounding=ROUND_DOWN
        ) * b
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "mode": "DEMO"
    }


@app.get("/api/status")
async def status():
    return {
        "ok": True,
        "bot": "SMC Binance Auto Trader V2",
        "status": "online",
        "mode": "DEMO",
        "auto": state["auto"],
        "emergency": state["emergency"],
        "active": state["active"],
    }


@app.post("/api/auto")
async def auto(a: Auto):

    if a.enabled and state["emergency"]:
        raise HTTPException(
            409,
            "Emergency stop is enabled. Restart backend to clear it."
        )

    state["auto"] = a.enabled

    return {
        "enabled": state["auto"],
        "mode": "DEMO"
    }


@app.post("/api/emergency-stop")
async def emergency():

    state["emergency"] = True
    state["auto"] = False

    return {
        "ok": True,
        "auto": False,
        "note": "New auto trades disabled."
    }


@app.post("/api/execute-signal")
async def execute(s: Signal):

    if state["emergency"]:
        raise HTTPException(
            409,
            "Emergency stop enabled"
        )

    if not state["auto"]:
        raise HTTPException(
            409,
            "AUTO TRADE OFF"
        )

    if state["active"]:
        raise HTTPException(
            409,
            "One trade is already active"
        )

    if s.side not in ("BUY", "SELL"):
        raise HTTPException(
            400,
            "Invalid side"
        )

    if s.quantity_usdt > MAX:
        raise HTTPException(
            400,
            f"Trade size exceeds {MAX} USDT"
        )

    # Prevent duplicate signal on the same candle
    k = f"{s.symbol}:{s.side}:{s.candle_time}"

    if state["last"] == k:
        raise HTTPException(
            409,
            "Duplicate signal"
        )

    # Get Binance symbol information
    inf = await info(s.symbol)

    lot = next(
        x for x in inf["filters"]
        if x["filterType"]
        in ("LOT_SIZE", "MARKET_LOT_SIZE")
    )

    qty = floorq(
        s.quantity_usdt / s.entry,
        float(lot["stepSize"])
    )

    if qty <= 0:
        raise HTTPException(
            400,
            "Quantity too small for symbol step size"
        )

    # Set leverage
    await req(
        "POST",
        "/fapi/v1/leverage",
        {
            "symbol": s.symbol,
            "leverage": LEV
        }
    )

    # MARKET ENTRY
    entry = await req(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": s.symbol,
            "side": s.side,
            "type": "MARKET",
            "quantity": qty,
            "newOrderRespType": "RESULT"
        },
    )

    close_side = (
        "SELL"
        if s.side == "BUY"
        else "BUY"
    )

    # STOP LOSS
    # Current Binance USD-M conditional order endpoint
    sl = await req(
        "POST",
        "/fapi/v1/algoOrder",
        {
            "algoType": "CONDITIONAL",
            "symbol": s.symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "triggerPrice": s.sl,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
        },
    )

    # TAKE PROFIT 2
    tp2 = await req(
        "POST",
        "/fapi/v1/algoOrder",
        {
            "algoType": "CONDITIONAL",
            "symbol": s.symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": s.tp2,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
        },
    )

    state["active"] = {
        "symbol": s.symbol,
        "side": s.side,
        "qty": qty,
        "entry_order_id": entry.get("orderId"),
        "sl_algo_id": sl.get("algoId"),
        "tp2_algo_id": tp2.get("algoId"),
        "entry_price": entry.get("avgPrice"),
        "sl_price": s.sl,
        "tp2_price": s.tp2,
    }

    state["last"] = k

    return {
        "ok": True,
        "mode": "DEMO",
        "active": state["active"]
    }


@app.post("/api/close")
async def close():

    if not state["active"]:
        return {
            "ok": True,
            "message": "No active trade"
        }

    a = state["active"]

    side = (
        "SELL"
        if a["side"] == "BUY"
        else "BUY"
    )

    r = await req(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": a["symbol"],
            "side": side,
            "type": "MARKET",
            "quantity": a["qty"]
        },
    )

    state["active"] = None

    return r
