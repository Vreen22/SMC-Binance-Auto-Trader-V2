import os
import time
import hmac
import hashlib
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

BASE = os.getenv(
    "BINANCE_BASE_URL",
    "https://demo-fapi.binance.com"
).rstrip("/")

MAX_USDT = float(os.getenv("MAX_USDT_PER_TRADE", "20"))
LEVERAGE = int(os.getenv("LEVERAGE", "1"))

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


def signed_params(params=None):
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
            "BINANCE_API_KEY or BINANCE_API_SECRET is missing"
        )

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(
            method,
            BASE + path,
            params=signed_params(params),
            headers={
                "X-MBX-APIKEY": KEY
            },
        )

    if response.status_code >= 400:
        raise HTTPException(
            response.status_code,
            response.text
        )

    return response.json()


async def public_get(path, params=None):
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            BASE + path,
            params=params or {}
        )

    if response.status_code >= 400:
        raise HTTPException(
            response.status_code,
            response.text
        )

    return response.json()


def floor_quantity(quantity, step_size):
    q = Decimal(str(quantity))
    step = Decimal(str(step_size))

    return float(
        (q / step).to_integral_value(
            rounding=ROUND_DOWN
        ) * step
    )


async def symbol_info(symbol):
    data = await public_get(
        "/fapi/v1/exchangeInfo"
    )

    for item in data.get("symbols", []):
        if item["symbol"] == symbol:
            return item

    raise HTTPException(
        400,
        f"Unknown Futures symbol: {symbol}"
    )


@app.get("/")
async def root():
    return {
        "bot": "SMC Binance Auto Trader V2",
        "status": "online",
        "mode": "DEMO"
    }


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


# SAFE API CONNECTION TEST
# This reads the Demo Futures account only.
# It does NOT open a trade.
@app.get("/api/account")
async def account():
    data = await req(
        "GET",
        "/fapi/v2/account"
    )

    return {
        "ok": True,
        "mode": "DEMO",
        "accountType": data.get("accountType"),
        "totalWalletBalance": data.get(
            "totalWalletBalance"
        ),
        "availableBalance": data.get(
            "availableBalance"
        ),
        "totalUnrealizedProfit": data.get(
            "totalUnrealizedProfit"
        ),
    }


@app.post("/api/auto")
async def set_auto(data: Auto):

    if data.enabled and state["emergency"]:
        raise HTTPException(
            409,
            "Emergency stop is enabled"
        )

    state["auto"] = data.enabled

    return {
        "ok": True,
        "enabled": state["auto"],
        "mode": "DEMO"
    }


@app.post("/api/emergency-stop")
async def emergency_stop():

    state["emergency"] = True
    state["auto"] = False

    return {
        "ok": True,
        "auto": False,
        "note": "New auto trades disabled."
    }


@app.post("/api/execute-signal")
async def execute_signal(signal: Signal):

    if state["emergency"]:
        raise HTTPException(
            409,
            "Emergency stop enabled"
        )

    if not state["auto"]:
        raise HTTPException(
            409,
            "AUTO TRADE is OFF"
        )

    if state["active"]:
        raise HTTPException(
            409,
            "One trade is already active"
        )

    if signal.side not in ("BUY", "SELL"):
        raise HTTPException(
            400,
            "Invalid side"
        )

    if signal.quantity_usdt > MAX_USDT:
        raise HTTPException(
            400,
            f"Trade size exceeds {MAX_USDT} USDT"
        )

    signal_key = (
        f"{signal.symbol}:"
        f"{signal.side}:"
        f"{signal.candle_time}"
    )

    if state["last"] == signal_key:
        raise HTTPException(
            409,
            "Duplicate signal"
        )

    info = await symbol_info(signal.symbol)

    lot_filter = next(
        (
            f for f in info["filters"]
            if f["filterType"]
            in ("LOT_SIZE", "MARKET_LOT_SIZE")
        ),
        None
    )

    if not lot_filter:
        raise HTTPException(
            400,
            "LOT_SIZE filter not found"
        )

    step_size = float(
        lot_filter["stepSize"]
    )

    quantity = floor_quantity(
        signal.quantity_usdt / signal.entry,
        step_size
    )

    if quantity <= 0:
        raise HTTPException(
            400,
            "Quantity is too small"
        )

    # Set leverage
    await req(
        "POST",
        "/fapi/v1/leverage",
        {
            "symbol": signal.symbol,
            "leverage": LEVERAGE
        }
    )

    # MARKET ENTRY
    entry_order = await req(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": signal.symbol,
            "side": signal.side,
            "type": "MARKET",
            "quantity": quantity,
            "newOrderRespType": "RESULT"
        }
    )

    close_side = (
        "SELL"
        if signal.side == "BUY"
        else "BUY"
    )

    # STOP LOSS
    sl_order = await req(
        "POST",
        "/fapi/v1/algoOrder",
        {
            "algoType": "CONDITIONAL",
            "symbol": signal.symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "triggerPrice": signal.sl,
            "closePosition": "true",
            "workingType": "MARK_PRICE"
        }
    )

    # TAKE PROFIT 2
    tp2_order = await req(
        "POST",
        "/fapi/v1/algoOrder",
        {
            "algoType": "CONDITIONAL",
            "symbol": signal.symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": signal.tp2,
            "closePosition": "true",
            "workingType": "MARK_PRICE"
        }
    )

    state["active"] = {
        "symbol": signal.symbol,
        "side": signal.side,
        "quantity": quantity,
        "entry_order_id": entry_order.get(
            "orderId"
        ),
        "sl_algo_id": sl_order.get(
            "algoId"
        ),
        "tp2_algo_id": tp2_order.get(
            "algoId"
        ),
        "entry_price": entry_order.get(
            "avgPrice"
        ),
        "sl_price": signal.sl,
        "tp1_price": signal.tp1,
        "tp2_price": signal.tp2,
    }

    state["last"] = signal_key

    return {
        "ok": True,
        "mode": "DEMO",
        "active": state["active"]
    }


@app.post("/api/close")
async def close_trade():

    if not state["active"]:
        return {
            "ok": True,
            "message": "No active trade"
        }

    active = state["active"]

    close_side = (
        "SELL"
        if active["side"] == "BUY"
        else "BUY"
    )

    result = await req(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": active["symbol"],
            "side": close_side,
            "type": "MARKET",
            "quantity": active["quantity"]
        }
    )

    state["active"] = None

    return {
        "ok": True,
        "mode": "DEMO",
        "result": result
    }
