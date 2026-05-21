# ─────────────────────────────────────────────
# TELEGRAM CONFIRMATION SYSTEM
# ─────────────────────────────────────────────
import os
import time
import requests  # FIX: explicit import (was missing in original)
from flask import request, jsonify  # FIX: moved to top-level, not inside route handler

# ── CONFIGURE THESE TWO ──────────────────────
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
# etc.
# ─────────────────────────────────────────────

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ─────────────────────────────────────────────
# SEND CONFIRMATION TO TELEGRAM WITH BUTTONS
# ─────────────────────────────────────────────

def send_telegram_confirmation(signal: dict, conf_id: str):
    """
    Sends a trade alert to Telegram with ✅ YES / ❌ NO inline buttons.
    Handles both OPTIONS signals and EQUITY/DELIVERY signals.
    """
    is_equity  = signal.get("is_equity", False)
    action_emoji = "🟢" if signal.get("action") == "BUY" else "🔴"
    exit_flag    = "🚪 EXIT SIGNAL" if signal.get("is_exit") else ""

    if is_equity:
        # ── EQUITY / DELIVERY signal ────────────────
        message = (
            f"🚨 *EQUITY SIGNAL DETECTED* {exit_flag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *{signal.get('tradingsymbol', '—')}* (NSE)\n"
            f"{action_emoji} *Action  :* {signal.get('action', '—')}\n"
            f"💰 *LTP     :* ₹{signal.get('ltp', '—')}\n"
            f"🛑 *SL      :* {signal.get('sl', '—')}\n"
            f"🎯 *Target  :* {signal.get('target', '—')}\n"
            f"📦 *Qty     :* {signal.get('qty', '—')} shares\n"
            f"💵 *Value   :* ₹{signal.get('order_value', '—')}\n"
            f"📋 *Type    :* DELIVERY (CNC)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 _{signal.get('raw_signal', '')[:120]}_"
        )
    else:
        # ── OPTIONS signal ──────────────────────────
        # FIX: lot size constant now comes from signal (was hardcoded 75 here vs 65 in main)
        lot_size = signal.get("lot_size", 75)
        qty_lots = signal.get("qty", 1)
        message = (
            f"🚨 *TRADE SIGNAL DETECTED* {exit_flag}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *{signal.get('instrument', 'NIFTY')} "
            f"{signal.get('strike', '—')} "
            f"{signal.get('option_type', '—')}*\n"
            f"{action_emoji} *Action :* {signal.get('action', '—')}\n"
            f"🛑 *SL     :* {signal.get('sl', '—')}\n"
            f"🎯 *Target :* {signal.get('target', '—')}\n"
            f"📦 *Lots   :* {qty_lots}\n"
            f"⚡ *Qty    :* {qty_lots * lot_size} units\n"
            f"📋 *Type   :* {'NRML (Swing)' if signal.get('is_swing') else 'MIS (Intraday)'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 _{signal.get('raw_signal', '')[:120]}_"
        )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ YES — Place Order", "callback_data": f"confirm:{conf_id}"},
            {"text": "❌ NO — Skip",         "callback_data": f"reject:{conf_id}"}
        ]]
    }

    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "Markdown",
        "reply_markup": keyboard
    }

    resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    if resp.ok:
        print(f"✅ Telegram alert sent — conf_id: {conf_id}")
    else:
        print(f"❌ Telegram send failed: {resp.text}")


def send_telegram_message(text: str):
    """Send a plain text message to Telegram."""
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "Markdown"
    })


def edit_telegram_message(chat_id, message_id, new_text: str):
    """Edit a Telegram message (used to update after confirm/reject)."""
    requests.post(f"{TELEGRAM_API}/editMessageText", json={
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       new_text,
        "parse_mode": "Markdown"
    })


# ─────────────────────────────────────────────
# UNIFIED send_confirmation_request
# FIX: signature now matches how main.py calls it — (signal, pending_confirmations)
# The old main.py had a LOCAL version with wrong signature that shadowed this.
# That local version is deleted in main.py.
# ─────────────────────────────────────────────

def send_confirmation_request(signal: dict, pending_confirmations: dict) -> str:
    """
    Stores signal in pending dict and fires Telegram alert with YES/NO buttons.
    Returns conf_id.
    """
    conf_id = f"CONF_{int(time.time())}"
    pending_confirmations[conf_id] = signal
    send_telegram_confirmation(signal, conf_id)
    return conf_id


# ─────────────────────────────────────────────
# TELEGRAM WEBHOOK ROUTES
# ─────────────────────────────────────────────

def register_telegram_routes(app, pending_confirmations,
                              place_option_order,
                              place_equity_order,
                              exit_all_nifty_positions):
    """
    Register Telegram webhook route on the Flask app.
    FIX: added place_equity_order parameter to handle equity signals.
    Call once after creating the Flask app.
    """

    @app.route("/webhook/telegram", methods=["POST"])
    def telegram_webhook():
        data = request.json  # FIX: request imported at top level now

        if "callback_query" not in data:
            return jsonify({"ok": True})

        cb         = data["callback_query"]
        cb_id      = cb["id"]
        cb_data    = cb.get("data", "")
        chat_id    = cb["message"]["chat"]["id"]
        message_id = cb["message"]["message_id"]
        user_name  = cb["from"].get("first_name", "Someone")

        # Ack the button tap (clears Telegram's spinner)
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery",
                      json={"callback_query_id": cb_id})

        try:
            action, conf_id = cb_data.split(":", 1)
        except ValueError:
            return jsonify({"ok": True})

        # ── CONFIRM ──────────────────────────────
        if action == "confirm":
            if conf_id not in pending_confirmations:
                edit_telegram_message(chat_id, message_id,
                    "⚠️ Confirmation expired or already handled.")
                return jsonify({"ok": True})

            signal = pending_confirmations.pop(conf_id)

            if signal.get("is_exit"):
                exit_all_nifty_positions()
                edit_telegram_message(chat_id, message_id,
                    f"🚪 *EXIT executed* by {user_name}\nAll Nifty positions closed.")

            elif signal.get("is_equity"):
                # FIX: route equity signals to equity order function
                result = place_equity_order(signal)
                if result.get("success"):
                    edit_telegram_message(chat_id, message_id,
                        f"✅ *Equity Order Placed!*\n"
                        f"Symbol : `{result.get('symbol')}`\n"
                        f"Qty    : {result.get('qty')} shares\n"
                        f"Order  : `{result.get('order_id')}`\n"
                        f"By     : {user_name}")
                else:
                    edit_telegram_message(chat_id, message_id,
                        f"❌ *Order FAILED*\nReason: {result.get('error')}")

            else:
                result = place_option_order(signal)
                if result.get("success"):
                    edit_telegram_message(chat_id, message_id,
                        f"✅ *Order Placed!*\n"
                        f"Symbol : `{result.get('symbol')}`\n"
                        f"Qty    : {result.get('qty')}\n"
                        f"Order  : `{result.get('order_id')}`\n"
                        f"By     : {user_name}")
                else:
                    edit_telegram_message(chat_id, message_id,
                        f"❌ *Order FAILED*\nReason: {result.get('error')}")

        # ── REJECT ───────────────────────────────
        elif action == "reject":
            pending_confirmations.pop(conf_id, None)
            edit_telegram_message(chat_id, message_id,
                f"❌ *Trade Rejected* by {user_name}\nSignal discarded.")

        return jsonify({"ok": True})


# ─────────────────────────────────────────────
# REGISTER TELEGRAM WEBHOOK WITH TELEGRAM
# ─────────────────────────────────────────────

def set_telegram_webhook(ngrok_url: str):
    """
    Tell Telegram where to send button-tap events.
    Call once after your ngrok URL is ready.
    Example: set_telegram_webhook("https://abc123.ngrok.io")
    """
    webhook_url = f"{ngrok_url}/webhook/telegram"
    resp = requests.post(f"{TELEGRAM_API}/setWebhook",
                         json={"url": webhook_url})
    print(f"Telegram webhook set: {resp.json()}")