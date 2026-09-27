import os
import time
import hmac
import hashlib
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
MAX_USDT = float(os.getenv("MAX_USDT_PER_TRADE", "20"))
LEVERAGE = int(os.getenv("LEVERAGE", "1"))

BASE_URL = (
    "https://testnet.binancefuture.com"
    if TESTNET
    else "https://fapi.binance.com"
)

app = FastAPI(title="SMC Binance Auto Trader V2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# BOT STATE
# -------------------------

state = {
    "auto": False,
    "emergency": False,
    "active_trade": None,
    "last_signal": None,
}


# -------------------------
# DATA MODELS
# -------------------------

class AutoTrade(BaseModel):
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


# -------------------------
# BINANCE SIGNATURE
# -------------------------

def signed_params(params=None):
    if params is None:
        params = {}

    params = dict(params)

    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000

    query = urlencode(params)

    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    params["signature"] = signature

    return params


# -------------------------
# BINANCE REQUEST
# -------------------------

async def binance_request(method, path, params=None):

    if not API_KEY or not API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Binance API credentials are missing."
        )

    headers = {
        "X-MBX-APIKEY": API_KEY
    }

    async with httpx.AsyncClient(timeout=20) as client:

        response = await client.request(
            method,
            BASE_URL + path,
            params=signed_params(params or {}),
            headers=headers,
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text,
        )

    return response.json()


# -------------------------
# EXCHANGE INFO
# -------------------------

async def get_symbol_info(symbol):

    async with httpx.AsyncClient(timeout=20) as client:

        response = await client.get(
            BASE_URL + "/fapi/v1/exchangeInfo"
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text,
        )

    data = response.json()

    for item in data["symbols"]:
        if item["symbol"] == symbol:
            return item

    raise HTTPException(
        status_code=400,
        detail=f"Unknown Futures symbol: {symbol}"
    )


# -------------------------
# QUANTITY ROUNDING
# -------------------------

def floor_quantity(quantity, step):

    q = Decimal(str(quantity))
    s = Decimal(str(step))

    result = (
        q / s
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * s

    return float(result)


# -------------------------
# HEALTH
# -------------------------

@app.get("/")
async def root():

    return {
        "bot": "SMC Binance Auto Trader V2",
        "status": "online",
        "mode": "TESTNET" if TESTNET else "LIVE",
    }


@app.get("/health")
async def health():

    return {
        "ok": True,
        "mode": "TESTNET" if TESTNET else "LIVE",
    }


# -------------------------
# BOT STATUS
# -------------------------

@app.get("/api/status")
async def status():

    return {
        "ok": True,
        "mode": "testnet" if TESTNET else "live",
        "auto": state["auto"],
        "emergency": state["emergency"],
        "active_trade": state["active_trade"],
    }


# -------------------------
# AUTO TRADE ON/OFF
# -------------------------

@app.post("/api/auto")
async def set_auto(data: AutoTrade):

    if data.enabled and state["emergency"]:

        raise HTTPException(
            status_code=409,
            detail="Emergency stop is active. Restart the backend first."
        )

    state["auto"] = data.enabled

    return {
        "ok": True,
        "auto": state["auto"],
    }


# -------------------------
# EMERGENCY STOP
# -------------------------

@app.post("/api/emergency-stop")
async def emergency_stop():

    state["emergency"] = True
    state["auto"] = False

    return {
        "ok": True,
        "message": "Emergency stop enabled.",
    }


# -------------------------
# OPEN AUTO TRADE
# -------------------------

@app.post("/api/execute-signal")
async def execute_signal(signal: Signal):

    # Safety checks
    if state["emergency"]:

        raise HTTPException(
            status_code=409,
            detail="Emergency stop is enabled."
        )

    if not state["auto"]:

        raise HTTPException(
            status_code=409,
            detail="AUTO TRADE is OFF."
        )

    if state["active_trade"] is not None:

        raise HTTPException(
            status_code=409,
            detail="A trade is already active."
        )

    if signal.side not in ["BUY", "SELL"]:

        raise HTTPException(
            status_code=400,
            detail="Side must be BUY or SELL."
        )

    if signal.quantity_usdt > MAX_USDT:

        raise HTTPException(
            status_code=400,
            detail=f"Trade size exceeds MAX_USDT_PER_TRADE={MAX_USDT}"
        )

    # Prevent duplicate signal
    signal_id = (
        f"{signal.symbol}:"
        f"{signal.side}:"
        f"{signal.candle_time}"
    )

    if state["last_signal"] == signal_id:

        raise HTTPException(
            status_code=409,
            detail="This signal was already executed."
        )

    # Get Binance symbol information
    symbol_info = await get_symbol_info(signal.symbol)

    lot_filter = next(
        (
            f
            for f in symbol_info["filters"]
            if f["filterType"] == "LOT_SIZE"
        ),
        None,
    )

    if lot_filter is None:

        raise HTTPException(
            status_code=500,
            detail="LOT_SIZE filter not found."
        )

    step_size = float(
        lot_filter["stepSize"]
    )

    # Calculate quantity
    raw_quantity = (
        signal.quantity_usdt /
        signal.entry
    )

    quantity = floor_quantity(
        raw_quantity,
        step_size
    )

    if quantity <= 0:

        raise HTTPException(
            status_code=400,
            detail="Calculated quantity is zero."
        )

    # -------------------------
    # SET LEVERAGE
    # -------------------------

    await binance_request(
        "POST",
        "/fapi/v1/leverage",
        {
            "symbol": signal.symbol,
            "leverage": LEVERAGE,
        },
    )

    # -------------------------
    # OPEN MARKET POSITION
    # -------------------------

    entry_order = await binance_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": signal.symbol,
            "side": signal.side,
            "type": "MARKET",
            "quantity": quantity,
            "newOrderRespType": "RESULT",
        },
    )

    # Opposite side closes the position
    close_side = (
        "SELL"
        if signal.side == "BUY"
        else "BUY"
    )

    # -------------------------
    # STOP LOSS
    # -------------------------

    sl_order = await binance_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": signal.symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": signal.sl,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
        },
    )

    # -------------------------
    # TP2 AUTO CLOSE
    # -------------------------

    tp2_order = await binance_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": signal.symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": signal.tp2,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
        },
    )

    # -------------------------
    # SAVE ACTIVE TRADE
    # -------------------------

    state["active_trade"] = {
        "symbol": signal.symbol,
        "side": signal.side,
        "quantity": quantity,
        "entry_order_id": entry_order.get("orderId"),
        "sl_order_id": sl_order.get("orderId"),
        "tp2_order_id": tp2_order.get("orderId"),
        "entry": signal.entry,
        "sl": signal.sl,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
    }

    state["last_signal"] = signal_id

    return {
        "ok": True,
        "message": "Trade opened with automatic SL and TP2.",
        "trade": state["active_trade"],
    }


# -------------------------
# MANUAL CLOSE
# -------------------------

@app.post("/api/close")
async def close_trade():

    if state["active_trade"] is None:

        return {
            "ok": True,
            "message": "No active trade."
        }

    trade = state["active_trade"]

    close_side = (
        "SELL"
        if trade["side"] == "BUY"
        else "BUY"
    )

    result = await binance_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": trade["symbol"],
            "side": close_side,
            "type": "MARKET",
            "quantity": trade["quantity"],
        },
    )

    state["active_trade"] = None

    return {
        "ok": True,
        "message": "Trade closed.",
        "result": result,
    }
