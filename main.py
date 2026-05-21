"""
WhatsApp Trade Signal Bot
=========================
Reads Nifty options + equity signals from WhatsApp group (via webhook/manual input),
parses them with Gemini Flash, sends Telegram confirmation, then places order on Angel One.

Flow:
  Signal text received
       ↓
  Gemini Flash parses → symbol, strike/tradingsymbol, action, SL, target, qty, is_equity
       ↓
  If equity → fetch LTP → compute qty = 5000 // ltp → DELIVERY (CNC) order
  If options → compute qty = lots × lot_size → MIS or NRML order
       ↓
  Telegram YES/NO confirmation sent to you
       ↓
  YES → Angel One SmartAPI places the order + SL order (options only)
       ↓
  Result edited back into Telegram message

Setup:
  pip install smartapi-python google-generativeai requests flask pyotp logzero
"""

import os
import re
import json
import time
import pyotp
import requests
from google import genai
from flask import Flask, request, jsonify
from SmartApi import SmartConnect
from logzero import logger
from datetime import datetime, timezone, timedelta
from telegram_confirmation import (
    send_confirmation_request,         # FIX: only import, don't redefine locally
    register_telegram_routes,
    set_telegram_webhook,
    send_telegram_message
)

from dotenv import load_dotenv
load_dotenv()


# ... etc
# ─────────────────────────────────────────────
# CONFIG — fill these in
# ─────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ANGEL_CLIENT_ID   = os.getenv("ANGEL_CLIENT_ID")
ANGEL_PASSWORD    = os.getenv("ANGEL_PASSWORD") #4 didgit login pin
ANGEL_API_KEY     = os.getenv("ANGEL_API_KEY") #after creating API 
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET") #after creating API 

client = genai.Client(api_key=GEMINI_API_KEY)

YOUR_WHATSAPP     = "REMOVED"

TARGET_GROUP_NAME = "REMOVED"


# FIX: Nifty lot size — NSE revised to 75 in 2024.
# Original had 65 in the constant but 75 in the Telegram message. Now consistent.
NIFTY_LOT_SIZE    = 75

DEFAULT_LOTS      = 1

# Budget for equity orders (qty = EQUITY_BUDGET // ltp)
EQUITY_BUDGET     = 5000


IST = timezone(timedelta(hours=5, minutes=30))  # FIX: IST timezone for market hours check


# ─────────────────────────────────────────────
# GEMINI SIGNAL PARSER
# FIX: prompt extended to handle equity picks (no CE/PE, no strike)
# ─────────────────────────────────────────────


client = genai.Client(api_key=GEMINI_API_KEY)

PARSE_PROMPT = """
You are a trading signal parser for Indian NSE markets (both equities and options).

A user will give you a WhatsApp message from a trading group.
Extract the trade details and return ONLY a valid JSON object with no markdown.

Determine signal type:
- If message mentions CE, PE, call, put, strike price, or Nifty/BankNifty options → it is an OPTIONS signal (is_equity = false)
- If message mentions a stock name/ticker without CE/PE context → it is an EQUITY signal (is_equity = true)

For OPTIONS signals:
- instrument: "NIFTY" or "BANKNIFTY" or as stated
- option_type: "CE" or "PE" — infer from context (bullish=CE, bearish=PE). null if unknown.
- strike: the strike price (number). null if not mentioned.
- expiry: expiry date string if mentioned, else null
- action: "BUY" or "SELL" or "EXIT"
- sl: stop loss level (index level)
- target: target level
- qty: number of lots (default 1 if not mentioned)
- is_swing: true if signal suggests holding overnight/swing, false for intraday (default false)
- is_exit: true if exit/close signal
- is_equity: false
- tradingsymbol: null
- confidence: 0-100

For EQUITY signals:
- tradingsymbol: NSE symbol like "RELIANCE-EQ", "TATAMOTORS-EQ" etc. Append -EQ.
- action: "BUY" or "SELL" or "EXIT"
- sl: stop loss price level
- target: target price level
- is_equity: true
- is_exit: true if exit signal
- is_swing: true (equity picks are usually delivery/swing)
- instrument: null
- option_type: null
- strike: null
- expiry: null
- qty: null (will be computed from LTP)
- confidence: 0-100

If message is NOT a trade signal (general chat), return:
{"confidence": 0, "is_exit": false, "is_equity": false}

Return format (fill all keys always):
{
  "instrument": "NIFTY",
  "option_type": "CE",
  "strike": 24200,
  "expiry": null,
  "action": "BUY",
  "sl": 24088,
  "target": 24340,
  "qty": 1,
  "is_exit": false,
  "is_equity": false,
  "is_swing": false,
  "tradingsymbol": null,
  "confidence": 90,
  "raw_signal": "<original message>"
}

Message to parse:
"""

import re

def parse_signal(message_text: str) -> dict:
    print(f"RAW INPUT: {repr(message_text)}") 
    """Regex-based parser — handles Ugesh's signal format reliably."""
    text = message_text.lower()

    # Exit signals
    is_exit = any(w in text for w in [
        "exit", "book profit", "sl hit", "stop loss hit",
        "book", "close", "square off"
    ])

    # SL
    sl_match = re.search(r"stop\s*loss[\s\-:]*(\d+)", text)
    sl = int(sl_match.group(1)) if sl_match else None

    # Target
    tgt_match = re.search(r"target[\s\-:]*(\d+)", text)
    target = int(tgt_match.group(1)) if tgt_match else None

    # Strike + option type together e.g. "24200 ce" or "24200ce"
    strike_match = re.search(r"\b(\d{4,5})\s*(ce|pe)\b", text)
    strike      = int(strike_match.group(1)) if strike_match else None
    option_type = strike_match.group(2).upper() if strike_match else None

    # Instrument
    instrument = "BANKNIFTY" if "banknifty" in text or "bank nifty" in text else "NIFTY"

    # Confidence
    confidence = 0
    if is_exit:
        confidence = 90
    elif sl and target:
        confidence = 90
    elif sl or target:
        confidence = 40

    return {
        "instrument":    instrument,
        "option_type":   option_type,
        "strike":        strike,
        "expiry":        None,
        "action":        "BUY",
        "sl":            sl,
        "target":        target,
        "qty":           1,
        "is_exit":       is_exit,
        "is_equity":     False,
        "is_swing":      False,
        "tradingsymbol": None,
        "confidence":    confidence,
        "raw_signal":    message_text
    }
# ─────────────────────────────────────────────
# ANGEL ONE — LOGIN & SESSION
# ─────────────────────────────────────────────

_angel_session = None

def get_angel_session():
    """Login to Angel One and return session object (cached)."""
    global _angel_session
    if _angel_session:
        return _angel_session
    try:
        obj = SmartConnect(api_key=ANGEL_API_KEY)
        totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()
        session = obj.generateSession(ANGEL_CLIENT_ID, ANGEL_PASSWORD, totp)
        if session["status"]:
            _angel_session = obj
            logger.info("✅ Angel One login successful")
        else:
            logger.error(f"Angel One login failed: {session}")
    except Exception as e:
        logger.error(f"Angel One connection error: {e}")
    return _angel_session


# ─────────────────────────────────────────────
# SCRIP MASTER — TOKEN LOOKUP
# FIX: Real lookup against Angel One's scrip master JSON.
# Download once at startup, search in-memory after that.
# ─────────────────────────────────────────────

_scrip_master = None   # list of dicts after load

def load_scrip_master():
    """Download Angel One scrip master JSON once and cache it."""
    global _scrip_master
    if _scrip_master is not None:
        return _scrip_master
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        _scrip_master = resp.json()
        logger.info(f"✅ Scrip master loaded — {len(_scrip_master)} instruments")
    except Exception as e:
        logger.error(f"Scrip master load failed: {e}")
        _scrip_master = []
    return _scrip_master


def get_token_for_symbol(tradingsymbol: str, exchange: str) -> str | None:
    """
    Lookup token for an equity symbol like 'RELIANCE-EQ' on NSE.
    Returns token string or None.
    """
    scrips = load_scrip_master()
    sym_upper = tradingsymbol.upper()
    exch_upper = exchange.upper()
    for s in scrips:
        if s.get("symbol", "").upper() == sym_upper and s.get("exch_seg", "").upper() == exch_upper:
            return str(s["token"])
    logger.warning(f"Token not found for {tradingsymbol} on {exchange}")
    return None


def get_nifty_option_token(instrument: str, strike: int, option_type: str, expiry_str: str) -> str | None:
    """
    FIX: Real scrip master lookup for NFO options.
    expiry_str format from Gemini: e.g. "08May2025" or "2025-05-08" — normalise below.
    Angel One NFO symbol format: NIFTY08MAY2524200CE
    Returns token string or None.
    """
    scrips = load_scrip_master()
    # Build partial symbol to match: e.g. "NIFTY" + strike + option_type
    # We search by name field containing strike and option type
    target_strike = str(int(strike))
    opt = option_type.upper()
    inst = instrument.upper()

    for s in scrips:
        if s.get("exch_seg", "") != "NFO":
            continue
        sym = s.get("symbol", "")
        # Symbol format: NIFTY08MAY2524200CE
        if sym.startswith(inst) and sym.endswith(target_strike + opt):
            # If expiry provided, also match expiry substring
            if expiry_str:
                # normalise expiry to uppercase 5-char: "08MAY" style
                expiry_check = expiry_str.upper().replace("-", "")[:5]
                if expiry_check not in sym.upper():
                    continue
            return str(s["token"])

    logger.warning(f"NFO token not found: {instrument} {strike} {option_type} {expiry_str}")
    return None


# ─────────────────────────────────────────────
# LTP FETCH
# ─────────────────────────────────────────────

def get_ltp(exchange: str, tradingsymbol: str, symboltoken: str) -> float | None:
    """
    Fetch Last Traded Price via Angel One getLtpData API.
    Returns float LTP or None on failure.
    """
    obj = get_angel_session()
    if not obj:
        return None
    try:
        resp = obj.ltpData(exchange, tradingsymbol, symboltoken)
        if resp and resp.get("status"):
            ltp = float(resp["data"]["ltp"])
            logger.info(f"LTP {tradingsymbol}: ₹{ltp}")
            return ltp
    except Exception as e:
        logger.error(f"LTP fetch error for {tradingsymbol}: {e}")
    return None


# ─────────────────────────────────────────────
# EQUITY ORDER PLACEMENT
# FIX: New function for equity/delivery orders.
# qty = EQUITY_BUDGET // ltp
# producttype = DELIVERY (CNC)
# ─────────────────────────────────────────────

def place_equity_order(signal: dict) -> dict:
    """
    Place a DELIVERY (CNC) BUY order for an equity stock.
    Qty = EQUITY_BUDGET // current LTP.
    """
    obj = get_angel_session()
    if not obj:
        return {"success": False, "error": "Angel One not connected"}

    tradingsymbol = signal.get("tradingsymbol")
    if not tradingsymbol:
        return {"success": False, "error": "No tradingsymbol in signal"}

    # Lookup token
    token = get_token_for_symbol(tradingsymbol, "NSE")
    if not token:
        return {"success": False, "error": f"Token not found for {tradingsymbol}"}

    # Fetch LTP to compute qty
    ltp = get_ltp("NSE", tradingsymbol, token)
    if not ltp:
        return {"success": False, "error": f"Could not fetch LTP for {tradingsymbol}"}

    qty = max(1, int(EQUITY_BUDGET // ltp))
    order_value = round(qty * ltp, 2)

    logger.info(f"Equity order: {tradingsymbol} | LTP ₹{ltp} | Qty {qty} | Value ₹{order_value}")

    try:
        order_params = {
            "variety":         "NORMAL",
            "tradingsymbol":   tradingsymbol,
            "symboltoken":     token,
            "transactiontype": signal.get("action", "BUY"),
            "exchange":        "NSE",
            "ordertype":       "MARKET",
            "producttype":     "DELIVERY",   # CNC
            "duration":        "DAY",
            "quantity":        str(qty),
            "price":           "0",
            "squareoff":       "0",
            "stoploss":        "0",
            "triggerprice":    "0",
        }
        response = obj.placeOrder(order_params)
        logger.info(f"Equity order placed: {response}")
        return {
            "success":     True,
            "order_id":    response,
            "symbol":      tradingsymbol,
            "qty":         qty,
            "ltp":         ltp,
            "order_value": order_value
        }
    except Exception as e:
        logger.error(f"Equity order placement error: {e}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────
# OPTIONS ORDER PLACEMENT
# FIX: producttype now respects is_swing flag (NRML vs MIS)
# FIX: real token lookup, not hardcoded placeholder
# ─────────────────────────────────────────────

def place_option_order(signal: dict) -> dict:
    """
    Place a Nifty options BUY order on Angel One.
    MIS for intraday, NRML (CARRYFORWARD) for swing.
    Follows up with an SL order after main order fills.
    """
    obj = get_angel_session()
    if not obj:
        return {"success": False, "error": "Angel One not connected"}

    instrument  = signal.get("instrument", "NIFTY")
    strike      = signal.get("strike")
    opt_type    = signal.get("option_type", "CE")
    expiry_str  = signal.get("expiry") or ""
    qty_lots    = signal.get("qty", DEFAULT_LOTS)
    qty_units   = qty_lots * NIFTY_LOT_SIZE
    is_swing    = signal.get("is_swing", False)
    product     = "CARRYFORWARD" if is_swing else "INTRADAY"

    token = get_nifty_option_token(instrument, strike, opt_type, expiry_str)
    if not token:
        return {"success": False, "error": f"Token not found for {instrument}{strike}{opt_type}"}

    # Build symbol for order — scrip master has the exact symbol, look it up
    # We re-search to get the exact tradingsymbol string Angel One expects
    scrips = load_scrip_master()
    symbol_name = f"{instrument}{strike}{opt_type}"   # fallback
    for s in scrips:
        if str(s.get("token")) == token:
            symbol_name = s.get("symbol", symbol_name)
            break

    try:
        order_params = {
            "variety":         "NORMAL",
            "tradingsymbol":   symbol_name,
            "symboltoken":     token,
            "transactiontype": "BUY",
            "exchange":        "NFO",
            "ordertype":       "MARKET",
            "producttype":     product,
            "duration":        "DAY",
            "quantity":        str(qty_units),
            "price":           "0",
            "squareoff":       "0",
            "stoploss":        "0",
            "triggerprice":    "0",
        }
        response = obj.placeOrder(order_params)
        logger.info(f"Options order placed: {response}")

        result = {
            "success":  True,
            "order_id": response,
            "symbol":   symbol_name,
            "qty":      qty_units,
            "token":    token
        }

        # FIX: place SL order after main order — with real computed SL price
        sl_result = place_option_sl_order(signal, symbol_name, token, qty_units, product)
        result["sl_order"] = sl_result

        return result

    except Exception as e:
        logger.error(f"Options order placement error: {e}")
        return {"success": False, "error": str(e)}


def place_option_sl_order(signal: dict, symbol_name: str, token: str,
                          qty_units: int, product: str) -> dict:
    """
    FIX: Place SL-M order after the main option order is placed.
    SL price is fetched from LTP and discounted by sl_pct (default 40%).
    The signal's index-level SL is stored for reference but option SL is LTP-based
    since we cannot directly map index SL to option price without Greeks.
    """
    obj = get_angel_session()
    if not obj:
        return {"success": False, "error": "Not connected"}

    # Fetch current option LTP to set a meaningful SL
    ltp = get_ltp("NFO", symbol_name, token)
    if not ltp:
        logger.warning("Could not fetch option LTP for SL — skipping SL order")
        return {"success": False, "error": "LTP fetch failed, SL not placed"}

    # Use 40% below current option LTP as trigger (conservative for intraday)
    sl_pct     = 0.40
    sl_trigger = round(ltp * (1 - sl_pct), 1)

    if sl_trigger <= 0:
        logger.warning(f"Computed SL trigger ≤ 0 ({sl_trigger}) — skipping SL order")
        return {"success": False, "error": "Invalid SL trigger price"}

    try:
        sl_params = {
            "variety":         "STOPLOSS",
            "tradingsymbol":   symbol_name,
            "symboltoken":     token,
            "transactiontype": "SELL",
            "exchange":        "NFO",
            "ordertype":       "STOPLOSS_MARKET",
            "producttype":     product,
            "duration":        "DAY",
            "quantity":        str(qty_units),
            "price":           "0",
            "squareoff":       "0",
            "stoploss":        "0",
            "triggerprice":    str(sl_trigger),
        }
        response = obj.placeOrder(sl_params)
        logger.info(f"SL order placed at trigger ₹{sl_trigger}: {response}")
        return {"success": True, "sl_order_id": response, "sl_trigger": sl_trigger}
    except Exception as e:
        logger.error(f"SL order error: {e}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────
# EXIT ALL NIFTY POSITIONS
# ─────────────────────────────────────────────

def exit_all_nifty_positions():
    """Exit all open Nifty option positions (called on EXIT signal)."""
    obj = get_angel_session()
    if not obj:
        return
    try:
        positions = obj.position()
        if positions["status"] and positions["data"]:
            for pos in positions["data"]:
                net_qty = int(pos.get("netqty", 0))
                if "NIFTY" in pos["tradingsymbol"] and net_qty != 0:
                    direction = "SELL" if net_qty > 0 else "BUY"
                    exit_params = {
                        "variety":         "NORMAL",
                        "tradingsymbol":   pos["tradingsymbol"],
                        "symboltoken":     pos["symboltoken"],
                        "transactiontype": direction,
                        "exchange":        "NFO",
                        "ordertype":       "MARKET",
                        "producttype":     "INTRADAY",
                        "duration":        "DAY",
                        "quantity":        str(abs(net_qty)),
                        "price":           "0",
                        "squareoff":       "0",
                        "stoploss":        "0",
                        "triggerprice":    "0",
                    }
                    obj.placeOrder(exit_params)
                    logger.info(f"Exited position: {pos['tradingsymbol']}")
    except Exception as e:
        logger.error(f"Exit error: {e}")


# ─────────────────────────────────────────────
# PENDING CONFIRMATIONS STORE
# ─────────────────────────────────────────────

pending_confirmations = {}


# ─────────────────────────────────────────────
# FLASK APP
# FIX: register_telegram_routes now takes place_equity_order too
# FIX: removed the local duplicate send_confirmation_request definition
# ─────────────────────────────────────────────

app = Flask(__name__)
register_telegram_routes(
    app,
    pending_confirmations,
    place_option_order,       # for options signals
    place_equity_order,       # for equity signals  ← NEW
    exit_all_nifty_positions
)


# ─────────────────────────────────────────────
# WHATSAPP WEBHOOK
# ─────────────────────────────────────────────
@app.route("/test-parse", methods=["POST"])
def test_parse():
    data = request.json
    message = data.get("message", "")
    result = parse_signal(message)
    return jsonify(result)

@app.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    data = request.json
    logger.info(f"Incoming webhook: {json.dumps(data)[:200]}")

    try:
        message_text = (
            data.get("entry", [{}])[0]
                .get("changes", [{}])[0]
                .get("value", {})
                .get("messages", [{}])[0]
                .get("text", {})
                .get("body", "")
        )
        sender = (
            data.get("entry", [{}])[0]
                .get("changes", [{}])[0]
                .get("value", {})
                .get("contacts", [{}])[0]
                .get("profile", {})
                .get("name", "")
        )
    except Exception:
        return jsonify({"status": "parse_error"}), 400

    if not message_text:
        return jsonify({"status": "empty_message"}), 200

    if TARGET_GROUP_NAME.lower() not in sender.lower():
        logger.info(f"Ignored message from: {sender}")
        return jsonify({"status": "ignored"}), 200

    process_signal_message(message_text)
    return jsonify({"status": "ok"}), 200


@app.route("/signal", methods=["POST"])
def manual_signal():
    """Manual signal input for testing. POST JSON: {"message": "..."}"""
    data = request.json
    message = data.get("message", "")
    if not message:
        return jsonify({"error": "No message provided"}), 400
    result = process_signal_message(message)
    return jsonify(result)


@app.route("/confirm/<conf_id>", methods=["POST"])
def confirm_trade(conf_id):
    """HTTP confirm endpoint (fallback — Telegram buttons are primary)."""
    if conf_id not in pending_confirmations:
        return jsonify({"error": "Invalid or expired confirmation ID"}), 404

    signal = pending_confirmations.pop(conf_id)
    logger.info(f"✅ Trade confirmed via HTTP: {conf_id}")

    if signal.get("is_exit"):
        exit_all_nifty_positions()
        return jsonify({"status": "exit_executed"})

    if signal.get("is_equity"):
        result = place_equity_order(signal)
    else:
        result = place_option_order(signal)

    return jsonify({"status": "order_placed", "result": result})


@app.route("/reject/<conf_id>", methods=["POST"])
def reject_trade(conf_id):
    """HTTP reject endpoint (fallback)."""
    pending_confirmations.pop(conf_id, None)
    return jsonify({"status": "rejected"})


@app.route("/positions", methods=["GET"])
def get_positions():
    obj = get_angel_session()
    if not obj:
        return jsonify({"error": "Not connected"}), 500
    positions = obj.position()
    return jsonify(positions)


@app.route("/pending", methods=["GET"])
def get_pending():
    return jsonify({"pending": list(pending_confirmations.keys()),
                    "count": len(pending_confirmations)})


# ─────────────────────────────────────────────
# CORE SIGNAL PROCESSING PIPELINE
# ─────────────────────────────────────────────

def process_signal_message(message_text: str) -> dict:
    """
    Main pipeline: parse → validate → enrich → confirm.
    Handles both options and equity signals.
    """
    logger.info(f"Processing: {message_text[:100]}")

    signal = parse_signal(message_text)

    if signal.get("confidence", 0) < 50:
        logger.info(f"Low confidence ({signal.get('confidence')}) — ignoring.")
        return {"status": "ignored", "confidence": signal.get("confidence")}

    # ── EXIT SIGNAL ──────────────────────────
    if signal.get("is_exit"):
        logger.info("Exit signal detected.")
        conf_id = send_confirmation_request(
            {**signal, "action": "EXIT ALL POSITIONS"},
            pending_confirmations
        )
        return {"status": "confirmation_pending", "conf_id": conf_id, "type": "exit"}

    # FIX: IST-aware market hours check (9:15 AM – 3:30 PM IST)
    """now_ist = datetime.now(IST)
    market_open  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    if not (market_open <= now_ist <= market_close):
        logger.warning(f"Outside market hours (IST {now_ist.strftime('%H:%M')}) — signal ignored")
        return {"status": "outside_market_hours", "ist_time": now_ist.strftime("%H:%M")}"""

    # ── EQUITY SIGNAL ────────────────────────
    if signal.get("is_equity"):
        tradingsymbol = signal.get("tradingsymbol")
        if not tradingsymbol:
            return {"status": "incomplete_signal", "missing": ["tradingsymbol"]}

        # Fetch LTP now so Telegram message shows accurate qty and value
        token = get_token_for_symbol(tradingsymbol, "NSE")
        ltp   = get_ltp("NSE", tradingsymbol, token) if token else None

        if ltp:
            qty         = max(1, int(EQUITY_BUDGET // ltp))
            order_value = round(qty * ltp, 2)
            signal["ltp"]         = ltp
            signal["qty"]         = qty
            signal["order_value"] = order_value
            signal["lot_size"]    = 1   # not used for equity but keeps template happy
        else:
            logger.warning(f"LTP unavailable for {tradingsymbol} — proceeding without qty preview")
            signal["ltp"]         = "N/A"
            signal["qty"]         = f"~{EQUITY_BUDGET}÷LTP"
            signal["order_value"] = "N/A"

        conf_id = send_confirmation_request(signal, pending_confirmations)
        return {"status": "confirmation_pending", "conf_id": conf_id,
                "type": "equity", "signal": signal}

# ── OPTIONS SIGNAL ───────────────────────
    # Auto-select ATM strike if not mentioned in signal
    if not signal.get("strike"):
        spot = get_ltp("NSE", "Nifty 50", "99926000")
        if spot:
            atm_strike = round(spot / 50) * 50
            signal["strike"] = atm_strike
            logger.info(f"Auto ATM strike selected: {atm_strike} (spot: {spot})")
        else:
            logger.warning("Could not fetch Nifty spot for ATM selection")

    # Ask user to clarify if option_type unknown
    if not signal.get("option_type"):
        send_telegram_message(
            f"⚠️ *Signal received but CE/PE unclear*\n\n"
            f"📝 _{signal.get('raw_signal', '')[:150]}_\n\n"
            f"Please resend with CE or PE mentioned."
        )
        return {"status": "awaiting_clarification", "reason": "option_type_unknown"}

    missing = [f for f in ["sl", "target"] if not signal.get(f)]
    if missing:
        logger.warning(f"Options signal missing fields: {missing} — skipping")
        return {"status": "incomplete_signal", "missing": missing}

    # Pass lot_size into signal so Telegram can display correct units
    signal["lot_size"] = NIFTY_LOT_SIZE

    conf_id = send_confirmation_request(signal, pending_confirmations)
    return {"status": "confirmation_pending", "conf_id": conf_id,
            "type": "options", "signal": signal}

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("WhatsApp Trade Signal Bot v2.0")
    print("Angel One + Gemini Flash")
    print("")
    print("Endpoints:")
    print("  POST /webhook/whatsapp  <- WhatsApp API sends messages here")
    print("  POST /signal            <- Manual test: {\"message\": \"...\"}")
    print("  POST /confirm/<id>      <- HTTP confirm")
    print("  POST /reject/<id>       <- HTTP reject")
    print("  GET  /positions         <- View open positions")
    print("  GET  /pending           <- View pending confirmations")
    print("")
    print("Starting server on http://localhost:5000")
    print("")
    # Pre-load scrip master at startup so first order isn't slow
    load_scrip_master()
    get_angel_session()
    app.run(debug=False, port=5000, host="0.0.0.0")