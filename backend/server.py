import os
import time
import hmac
import hashlib
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# BINANCE DEMO CONFIG
# =========================================================

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

BASE_URL = os.getenv(
    "BINANCE_BASE_URL",
    "https://demo-fapi.binance.com"
).rstrip("/")

MAX_USDT_PER_TRADE = float(
    os.getenv("MAX_USDT_PER_TRADE", "20")
)

LEVERAGE = int(
    os.getenv("LEVERAGE", "1")
)

# =========================================================
# APP
# =========================================================

app = FastAPI(
    title="SMC Binance Auto Trader V2"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# STATE
# =========================================================

state = {
    "auto": False,
    "emergency": False,
    "active": None,
    "last_signal": None,
}

# =========================================================
# MODELS
# =========================================================

class AutoRequest(BaseModel):
    enabled: bool


class SignalRequest(BaseModel):
    symbol: str
    side: str
    entry: float
    sl: float
    tp1: float
    tp2: float
    quantity_usdt: float = Field(gt=0)
    candle_time: int


# =========================================================
# BINANCE SIGNATURE
# =========================================================

def signed_params(params=None):

    if not API_KEY or not API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="BINANCE_API_KEY or BINANCE_API_SECRET is missing"
        )

    data = dict(params or {})

    data["timestamp"] = int(
        time.time() * 1000
    )

    data["recvWindow"] = 5000

    query = urlencode(data)

    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    data["signature"] = signature

    return data


# =========================================================
# PRIVATE BINANCE REQUEST
# =========================================================

async def binance_private(
    method: str,
    path: str,
    params=None
):

    if not API_KEY or not API_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Binance API credentials are missing"
        )

    async with httpx.AsyncClient(
        timeout=20
    ) as client:

        response = await client.request(
            method,
            BASE_URL + path,
            params=signed_params(params),
            headers={
                "X-MBX-APIKEY": API_KEY
            },
        )

    if response.status_code >= 400:

        try:
            error = response.json()
        except Exception:
            error = response.text

        raise HTTPException(
            status_code=response.status_code,
            detail=error
        )

    return response.json()


# =========================================================
# PUBLIC BINANCE REQUEST
# =========================================================

async def binance_public(
    path: str,
    params=None
):

    async with httpx.AsyncClient(
        timeout=20
    ) as client:

        response = await client.get(
            BASE_URL + path,
            params=params or {}
        )

    if response.status_code >= 400:

        try:
            error = response.json()
        except Exception:
            error = response.text

        raise HTTPException(
            status_code=response.status_code,
            detail=error
        )

    return response.json()


# =========================================================
# QUANTITY ROUNDING
# =========================================================

def floor_quantity(
    quantity: float,
    step_size: float
):

    q = Decimal(str(quantity))
    step = Decimal(str(step_size))

    result = (
        q / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step

    return float(result)


# =========================================================
# SYMBOL INFO
# =========================================================

async def get_symbol_info(symbol: str):

    data = await binance_public(
        "/fapi/v1/exchangeInfo"
    )

    for item in data.get("symbols", []):

        if item.get("symbol") == symbol:
            return item

    raise HTTPException(
        status_code=400,
        detail=f"Unknown Futures symbol: {symbol}"
    )


# =========================================================
# FRONTEND
# =========================================================

@app.get("/")
async def frontend():

    index_file = (
        Path(__file__).resolve().parent.parent
        / "index.html"
    )

    if not index_file.exists():

        return {
            "bot": "SMC Binance Auto Trader V2",
            "status": "online",
            "mode": "DEMO",
            "error": "index.html not found"
        }

    return FileResponse(
        index_file,
        media_type="text/html"
    )


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
async def health():

    return {
        "ok": True,
        "mode": "DEMO"
    }


# =========================================================
# STATUS
# =========================================================

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


# =========================================================
# ACCOUNT CONNECTION TEST
# =========================================================

@app.get("/api/account")
async def account():

    data = await binance_private(
        "GET",
        "/fapi/v2/account"
    )

    return {
        "ok": True,
        "mode": "DEMO",
        "accountType": data.get(
            "accountType"
        ),
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


# =========================================================
# AUTO TRADE
# =========================================================

@app.post("/api/auto")
async def set_auto(
    request: AutoRequest
):

    if request.enabled and state["emergency"]:

        raise HTTPException(
            status_code=409,
            detail="Emergency stop is enabled"
        )

    state["auto"] = request.enabled

    return {
        "ok": True,
        "enabled": state["auto"],
        "mode": "DEMO"
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
        "auto": False,
        "emergency": True,
        "note": "New auto trades disabled."
    }


# =========================================================
# EXECUTE SMC SIGNAL
# =========================================================

@app.post("/api/execute-signal")
async def execute_signal(
    signal: SignalRequest
):

    # -----------------------------------------------------
    # Safety checks
    # -----------------------------------------------------

    if state["emergency"]:

        raise HTTPException(
            status_code=409,
            detail="Emergency stop enabled"
        )

    if not state["auto"]:

        raise HTTPException(
            status_code=409,
            detail="AUTO TRADE is OFF"
        )

    if state["active"]:

        raise HTTPException(
            status_code=409,
            detail="One trade is already active"
        )

    if signal.side not in (
        "BUY",
        "SELL"
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid side"
        )

    if signal.quantity_usdt > MAX_USDT_PER_TRADE:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Trade size exceeds "
                f"{MAX_USDT_PER_TRADE} USDT"
            )
        )

    # -----------------------------------------------------
    # Duplicate signal protection
    # -----------------------------------------------------

    signal_key = (
        f"{signal.symbol}:"
        f"{signal.side}:"
        f"{signal.candle_time}"
    )

    if state["last_signal"] == signal_key:

        raise HTTPException(
            status_code=409,
            detail="Duplicate signal"
        )

    # -----------------------------------------------------
    # Symbol information
    # -----------------------------------------------------

    symbol = await get_symbol_info(
        signal.symbol
    )

    lot_filter = None

    for item in symbol.get(
        "filters",
        []
    ):

        if item.get(
            "filterType"
        ) in (
            "LOT_SIZE",
            "MARKET_LOT_SIZE"
        ):

            lot_filter = item
            break

    if not lot_filter:

        raise HTTPException(
            status_code=400,
            detail="LOT_SIZE filter not found"
        )

    step_size = float(
        lot_filter["stepSize"]
    )

    # -----------------------------------------------------
    # Calculate quantity
    # -----------------------------------------------------

    quantity = floor_quantity(
        signal.quantity_usdt /
        signal.entry,
        step_size
    )

    if quantity <= 0:

        raise HTTPException(
            status_code=400,
            detail="Quantity is too small"
        )

    # -----------------------------------------------------
    # Set leverage
    # -----------------------------------------------------

    await binance_private(
        "POST",
        "/fapi/v1/leverage",
        {
            "symbol": signal.symbol,
            "leverage": LEVERAGE
        }
    )

    # -----------------------------------------------------
    # MARKET ENTRY
    # -----------------------------------------------------

    entry_order = await binance_private(
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

    # -----------------------------------------------------
    # Close side
    # -----------------------------------------------------

    close_side = (
        "SELL"
        if signal.side == "BUY"
        else "BUY"
    )

    # -----------------------------------------------------
    # STOP LOSS
    # -----------------------------------------------------

    sl_order = await binance_private(
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

    # -----------------------------------------------------
    # TAKE PROFIT 2
    # -----------------------------------------------------

    tp2_order = await binance_private(
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

    # -----------------------------------------------------
    # Save active trade
    # -----------------------------------------------------

    state["active"] = {

        "symbol": signal.symbol,

        "side": signal.side,

        "quantity": quantity,

        "entry_order_id":
            entry_order.get(
                "orderId"
            ),

        "sl_algo_id":
            sl_order.get(
                "algoId"
            ),

        "tp2_algo_id":
            tp2_order.get(
                "algoId"
            ),

        "entry_price":
            entry_order.get(
                "avgPrice"
            ),

        "sl_price":
            signal.sl,

        "tp1_price":
            signal.tp1,

        "tp2_price":
            signal.tp2,
    }

    state["last_signal"] = signal_key

    return {
        "ok": True,
        "mode": "DEMO",
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
            "message": "No active trade"
        }

    active = state["active"]

    close_side = (
        "SELL"
        if active["side"] == "BUY"
        else "BUY"
    )

    result = await binance_private(
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
