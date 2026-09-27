import os
import time
import hmac
import hashlib
from pathlib import Path
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

BASE_URL = (
    "https://testnet.binancefuture.com"
    if TESTNET
    else "https://fapi.binance.com"
)

MAX_USDT = float(os.getenv("MAX_USDT_PER_TRADE", "20"))
LEVERAGE = int(os.getenv("LEVERAGE", "1"))

app = FastAPI(title="SMC Binance Auto Trader V2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Project root
ROOT = Path(__file__).resolve().parent.parent

# Runtime state
state = {
    "auto": False,
    "emergency": False,
    "active": None,
    "last": None,
}


# =========================================================
# FRONTEND
# =========================================================

@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(ROOT / "index.html")


# =========================================================
# MODELS
# =========================================================

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


# =========================================================
# BINANCE SIGNING
# =========================================================

def signed_params(params=None):
    if not API_KEY or not API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Binance API credentials are not configured."
        )

    params = dict(params or {})

    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000

    query = urlencode(params)

    signature = hmac.new(
        API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

    params["signature"] = signature

    return params


# =========================================================
# BINANCE REQUEST
# =========================================================

async def binance_request(method, path, params=None):

    signed = signed_params(params)

    async with httpx.AsyncClient(timeout=15) as client:

        response = await client.request(
            method,
            BASE_URL + path,
            params=signed,
            headers={
                "X-MBX-APIKEY": API_KEY
            }
        )

    if response.status_code >= 400:

        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    return response.json()


# =========================================================
# EXCHANGE INFO
# =========================================================

async def exchange_info(symbol):

    async with httpx.AsyncClient(timeout=15) as client:

        response = await client.get(
            BASE_URL + "/fapi/v1/exchangeInfo"
        )

    if response.status_code >= 400:

        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    data = response.json()

    for item in data["symbols"]:

        if item["symbol"] == symbol:
            return item

    raise HTTPException(
        status_code=400,
        detail="Unknown Futures symbol"
    )


# =========================================================
# QUANTITY ROUNDING
# =========================================================

def floor_quantity(quantity, step_size):

    a = Decimal(str(quantity))
    b = Decimal(str(step_size))

    result = (
        a / b
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * b

    return float(result)


# =========================================================
# STATUS
# =========================================================

@app.get("/api/status")
async def status():

    return {
        "ok": True,
        "mode": "TESTNET" if TESTNET else "LIVE",
        "auto": state["auto"],
        "emergency": state["emergency"],
        "active": state["active"],
    }


# =========================================================
# AUTO TRADE
# =========================================================

@app.post("/api/auto")
async def auto_trade(settings: Auto):

    if settings.enabled and state["emergency"]:

        raise HTTPException(
            status_code=409,
            detail="Emergency stop is enabled."
        )

    state["auto"] = settings.enabled

    return {
        "ok": True,
        "enabled": state["auto"]
    }


# =========================================================
# EMERGENCY STOP
# =========================================================

@app.post("/api/emergency-stop")
async def emergency_stop():

    state["emergency"] = True
    state["auto"] = False

    return {
        "ok": True,
        "emergency": True,
        "auto": False
    }


# =========================================================
# EXECUTE SIGNAL
# =========================================================

@app.post("/api/execute-signal")
async def execute_signal(signal: Signal):

    if state["emergency"]:

        raise HTTPException(
            status_code=409,
            detail="Emergency stop enabled."
        )

    if not state["auto"]:

        raise HTTPException(
            status_code=409,
            detail="AUTO TRADE OFF."
        )

    if state["active"]:

        raise HTTPException(
            status_code=409,
            detail="One trade is already active."
        )

    if signal.side not in ("BUY", "SELL"):

        raise HTTPException(
            status_code=400,
            detail="Invalid side."
        )

    if signal.quantity_usdt > MAX_USDT:

        raise HTTPException(
            status_code=400,
            detail=f"Trade size exceeds {MAX_USDT} USDT."
        )

    signal_key = (
        f"{signal.symbol}:"
        f"{signal.side}:"
        f"{signal.candle_time}"
    )

    if state["last"] == signal_key:

        raise HTTPException(
            status_code=409,
            detail="Duplicate signal."
        )

    # Get exchange information
    symbol_info = await exchange_info(
        signal.symbol
    )

    lot_filter = next(
        item
        for item in symbol_info["filters"]
        if item["filterType"] == "LOT_SIZE"
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
            status_code=400,
            detail="Quantity too small."
        )

    # Set leverage
    await binance_request(
        "POST",
        "/fapi/v1/leverage",
        {
            "symbol": signal.symbol,
            "leverage": LEVERAGE
        }
    )

    # Market entry
    entry_order = await binance_request(
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

    # Stop Loss
    sl_order = await binance_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": signal.symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": signal.sl,
            "closePosition": "true",
            "workingType": "MARK_PRICE"
        }
    )

    # TP2
    tp_order = await binance_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": signal.symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": signal.tp2,
            "closePosition": "true",
            "workingType": "MARK_PRICE"
        }
    )

    state["active"] = {
        "symbol": signal.symbol,
        "side": signal.side,
        "qty": quantity,
        "entry_order": entry_order.get("orderId"),
        "sl_order": sl_order.get("orderId"),
        "tp2_order": tp_order.get("orderId"),
        "entry_price": signal.entry,
        "sl": signal.sl,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
    }

    state["last"] = signal_key

    return {
        "ok": True,
        "mode": "TESTNET" if TESTNET else "LIVE",
        "active": state["active"]
    }


# =========================================================
# MANUAL CLOSE
# =========================================================

@app.post("/api/close")
async def close_trade():

    if not state["active"]:

        return {
            "ok": True,
            "message": "No active trade."
        }

    active = state["active"]

    close_side = (
        "SELL"
        if active["side"] == "BUY"
        else "BUY"
    )

    result = await binance_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": active["symbol"],
            "side": close_side,
            "type": "MARKET",
            "quantity": active["qty"]
        }
    )

    state["active"] = None

    return result


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
async def health():

    return {
        "ok": True,
        "mode": "TESTNET" if TESTNET else "LIVE"
    }
