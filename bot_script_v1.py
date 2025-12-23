import os
import time
import datetime
import requests
import ast
from dotenv import load_dotenv
from types import SimpleNamespace
import ccxt
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from datetime import datetime, timezone
import re

def extract_slug(url: str) -> str:
    """
    Extracts the slug like 'btc-updown-15m-1766290500' from a Polymarket event URL.
    """
    match = re.search(r"event/([a-zA-Z0-9\-]+)\?", url)
    if match:
        return match.group(1)
    return None

# =========================================================
# 1. HARDCODED TARGET SETTINGS
# =========================================================
TARGET_SLUG = "https://polymarket.com/event/btc-updown-15m-1766296800?tid=1766296601866"

TARGET_USDC_PER_LEG = 5.00
TARGET_PAIR_COST = 0.85
ANCHOR_PRICE = 0.30
KILL_SWITCH_MINUTE = 11

# =========================================================
# 2. BOT SETUP
# =========================================================
load_dotenv()

PK = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")

temp_client = ClobClient("https://clob.polymarket.com", key=PK, chain_id=137, signature_type=2, funder=FUNDER)
CREDS = temp_client.create_or_derive_api_creds()

client = ClobClient(
    host="https://clob.polymarket.com",
    key=PK,
    chain_id=137,
    funder=FUNDER,
    signature_type=2
)
client.set_api_creds(CREDS)

exchange = ccxt.binance()

# =========================================================
# 3. UTILITY FUNCTIONS
# =========================================================
def get_market_by_slug(slug):
    print(f"Fetching market data for slug: {slug}...")
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print(f"Gamma error {resp.status_code}")
            return None, None, None

        data = resp.json()

        outcomes = data.get("outcomes")
        token_ids = data.get("clobTokenIds")
        # Extract the end date (this is what was missing)
        end_time_str = data.get("endDate") or data.get("end_date_iso")

        if isinstance(outcomes, str):
            outcomes = ast.literal_eval(outcomes)
        if isinstance(token_ids, str):
            token_ids = ast.literal_eval(token_ids)

        tokens = {outcomes[i]: token_ids[i] for i in range(len(outcomes))}
        return tokens, data.get("question"), end_time_str

    except Exception as e:
        print(f"Slug lookup error: {e}")
        return None, None, None

def get_live_bid(token_id):
    """
    Fetches the best bid (price you can buy cheaply).
    """
    try:
        # side="SELL" gives the price a buyer is offering (the Bid)
        resp = client.get_price(token_id, side="SELL")
        return float(resp.get("price")) if resp else None
    except:
        return None

def is_filled(order_resp):
    """
    Returns True if the order is fully filled.
    Handles different CLOB response formats.
    """
    if not order_resp:
        return False

    # If 'status' exists at top level
    s = order_resp.get("status")
    if s == "FILLED":
        return True

    # If 'state' exists at top level
    s = order_resp.get("state")
    if s == "FILLED":
        return True

    # If nested inside 'order'
    order = order_resp.get("order")
    if order and order.get("status") == "FILLED":
        return True

    return False

# =========================================================
# 4. MAIN ARB LOOP
# =========================================================
def start_arb_loop():
    tokens, market_name, end_time_str = get_market_by_slug(extract_slug(TARGET_SLUG))

    if not tokens or "Up" not in tokens or "Down" not in tokens:
        print("❌ Critical Error: Could not resolve Token IDs.")
        return
    if not tokens or not end_time_str:
        print("❌ Critical Error: Could not resolve market details.")
        return

    # Polymarket uses 'Z' for UTC, which fromisoformat handles as '+00:00'
    market_end_dt = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))

    up_id, down_id = tokens["Up"], tokens["Down"]
    print(f"✅ SNIPER ACTIVE: {market_name}")
    print(f"IDs -> UP: {up_id} | DOWN: {down_id}")

    leg1_filled = False
    leg2_filled = False
    leg2_order_id = None
    start_time = time.time()

    while True:
        # 3. CALCULATE REAL TIME REMAINING
        now = datetime.now(timezone.utc)
        time_left = market_end_dt - now
        seconds_left = time_left.total_seconds()
        # Minutes elapsed since the start of a standard 15m window 
        # (Total 15 - remaining)
        current_minute = 15 - (seconds_left / 60)

        # --- KILL SWITCH ---
        if seconds_left <= (15 - KILL_SWITCH_MINUTE) * 60:
            print(f"\n⛔ Warning: Only {seconds_left/60:.1f}m left in window.")

            if leg1_filled and not leg2_filled and leg2_order_id:
                print(f"🔥 KILL SWITCH - PANIC SELL: Liquidating {leg1_side}")
                try:
                    client.cancel_order(leg2_order_id)
                    print(f"PANIC SELL SUCCESSFUL")
                except: pass

            # 2. Sell off Leg 1 if it was filled but Leg 2 wasn't (the "Arb" failed)
            if leg1_filled and not leg2_filled:
                try:
                    # Fetch the current best bid to ensure we fill immediately
                    # Note: We sell at a lower price just to exit (Market Sell)
                    exit_bid = get_live_bid(leg1_token_id)
                    sell_price = exit_bid if exit_bid else 0.05 # Panic price if bid is gone
                    print(f"🔥 KILL SWITCH - PANIC SELL: Liquidating {leg1_side} @ ${sell_price}")
                
                    sell_order = client.create_order(OrderArgs(
                        price=sell_price,
                        size=TARGET_USDC_PER_LEG,
                        side="SELL",  # This is the Sell instruction
                        token_id=leg1_token_id
                    ))
                    
                    sell_resp = client.post_order(sell_order)
                    print(f"📉 Sell response: {sell_resp}")
                except Exception as e:
                    print(f"❌ Panic Sell Failed: {e}")
        else:
            try:
                # Fetch Live Bids
                bid_up = get_live_bid(up_id)
                bid_down = get_live_bid(down_id)
                
                # Monitoring log
                print(f"📖 Time Left: {seconds_left//60:.0f}m {seconds_left%60:.0f}s | ⬆️ UP Bid: {bid_up or 'N/A'} | ⬇️ DOWN Bid: {bid_down or 'N/A'} | Leg1_filled: {leg1_filled} | Leg2_filled: {leg2_filled}")
                if (leg1_filled and leg2_filled):
                    print("\n💰 ARB LOCKED! Both sides filled.")

                # --- LEG 1 ENTRY ---
                if not leg1_filled:
                    candidates = []

                    # We need both prices to verify the arbitrage exists right now
                    if bid_up and bid_down:
                        current_total_cost = bid_up + bid_down
                        
                        # ARBITRAGE CONDITION: Total cost must be less than our target
                        if current_total_cost <= TARGET_PAIR_COST:
                            # We find the 'best' leg to start with (usually the cheaper one)
                            if bid_up <= bid_down:
                                l1_size = round(TARGET_USDC_PER_LEG / bid_up, 2)
                                candidates.append(("Up", bid_up, up_id, down_id, l1_size, bid_down))
                            else:
                                l1_size = round(TARGET_USDC_PER_LEG / bid_down, 2)
                                candidates.append(("Down", bid_down, down_id, up_id, l1_size, bid_up))
                    # if bid_up and bid_up <= ANCHOR_PRICE:
                    #     # Calculate size needed to spend TARGET_USDC_PER_LEG
                    #     # Example: $5 / 0.25 price = 20 shares
                    #     l1_size = round(TARGET_USDC_PER_LEG / bid_up, 2)
                    #     candidates.append(("Up", bid_up, up_id, down_id, l1_size))
                    # if bid_down and bid_down <= ANCHOR_PRICE:
                    #     l1_size = round(TARGET_USDC_PER_LEG / bid_down, 2)
                    #     candidates.append(("Down", bid_down, down_id, up_id, l1_size))

                    if candidates:
                        # Pick the best price among candidates
                        l1_side, l1_price, l1_tid, l2_tid, l1_size = min(candidates, key=lambda x: x[1])
                        # Polymarket prices are always 2 decimals max ($0.01 to $0.99)
                        clean_price = round(float(l1_price), 2)

                        # Size must be an integer or a very clean float
                        clean_size = round(float(TARGET_USDC_PER_LEG), 2)
                        print(f"\n🚀 ENTRY: Buying {l1_size} {l1_side} @ ${l1_price} (Cost: ~${TARGET_USDC_PER_LEG})")
                        order = client.create_order(OrderArgs(
                            price=round(float(l1_price), 2),
                            size=float(l1_size),
                            side="BUY",
                            token_id=str(l1_tid)
                        ))
                        print(f"order: {order}")
                        print(f"📡 Dispatching Order to CLOB...")
                        resp = client.post_order(order)
                        if resp is None:
                            print("❌ CRITICAL: post_order returned None. This is usually a local validation error.")
                        else:
                            print(f"Raw API Response: {resp}")
                            if resp.get("success"):
                                print("✅ Order Accepted!")
                                leg1_filled = True
                            else:
                                print(f"❌ Order Rejected: {resp.get('errorMsg')}")
                        if not resp.get("success"):
                            print(f"❌ Order Failed: {resp.get('errorMsg')}")
                        else:
                            # Most common: 'Unauthorized/Invalid api key' or 'not enough balance'
                            error_msg = resp.get('errorMsg') if resp else "No response from server"
                            print(f"❌ Order Failed. Reason: {error_msg}")
                        if resp.get("success"):
                            leg1_filled = True
                            # --- CALCULATE LEG 2 (Hedge) ---
                            # 1. Price is the remainder of your pair target
                            hedge_price = round(TARGET_PAIR_COST - l1_price, 2)
                            # 2. Size is calculated to spend the SAME USDC amount
                            hedge_size = round(TARGET_USDC_PER_LEG / hedge_price, 2)
                            print(f"⚓ HEDGING: Buying {hedge_size} shares @ ${hedge_price} (Cost: ~${TARGET_USDC_PER_LEG})")

                            h_order = client.create_order(OrderArgs(
                                price=float(hedge_price),
                                size=float(hedge_size),
                                side="BUY",
                                token_id=str(l2_tid)
                            ))
                            leg2_order_id = client.post_order(h_order).get("orderID")

                # --- LEG 2 FILL CHECK ---
                if leg1_filled and not leg2_filled and leg2_order_id:
                    order_status = client.get_order(leg2_order_id)
                    if is_filled(order_status):
                        print("\n💰 ARB LOCKED! Both sides filled.")
                        leg2_filled = True
            except Exception as e:
                time.sleep(1) # Simple retry delay
                print(f"💥 Exception occurred: {e}")
            time.sleep(1)

if __name__ == "__main__":
    print("=== POLYMARKET 15M ARB BOT INITIALIZED ===")
    start_arb_loop()
