import os
import time
import datetime

from dotenv import load_dotenv
from types import SimpleNamespace
import ccxt
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from datetime import datetime, timezone
import utils
import argparse

# =========================================================
# 1. HARDCODED TARGET SETTINGS
# =========================================================
TARGET_SLUG = "https://polymarket.com/event/btc-updown-15m-1766311200?tid=1766310946447"

# amount of USDC to fill in each purchase
TARGET_USDC_PER_LEG = 5.00
# TARGET_PAIR_COST = leg1_cost + leg2_cost
# must be < 1 for a profit
TARGET_PAIR_COST = 0.90
# buffer threshold to fill leg2
PATIENCE_BUFFER = 0.02
# the price where leg1 is to be filled
ANCHOR_PRICE = 0.30
# when time left is below KILL_SWITCH_MINUTE, execute KILL_SWITCH(ie. if only leg1 is filled, cancel leg1 and leg2)
KILL_SWITCH_MINUTE = 5
# if leg1 is not filled when there's ABORT_TRADE_WINDOW_MINUTE left, abort current trade session
ABORT_TRADE_WINDOW_MINUTE = 9

# =========================================================
# 2. Polymarket ClobClient SETUP
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
# 3. MAIN ARB LOOP
# =========================================================
def start_arb_loop():
    event_id = get_event_id()
    # Get token details, name of current market and end time
    # sample token: {'Up': '30153181842485352904197017700459535515576600216562347098287653572010919110026', 'Down': '34727604537658227042683904687799441550554770516513901542888684177893436283551'}
    # sample market_name: Bitcoin Up or Down - December 21, 5:00AM-5:15AM ET
    # sample end time: 2025-12-21T10:15:00Z
    tokens, market_name, end_time = utils.get_market_by_slug(utils.extract_slug(event_id))

    # If either of above details are missing, abort.
    if not tokens or "Up" not in tokens or "Down" not in tokens or not end_time:
        print("❌ Critical Error: Could not resolve market details.")
        print(f"tokens: {tokens}")
        print(f"market_name: {market_name}")
        print(f"end_time: {end_time}")
        return

    # Polymarket uses 'Z' for UTC, which fromisoformat handles as '+00:00'
    market_end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    up_id, down_id = tokens["Up"], tokens["Down"]

    print(f"🟢 Arbitrage ACTIVE: {market_name}")
    print(f"🟢 Arbitrage IDs -> UP: {up_id} | DOWN: {down_id}")

    leg1_filled = False
    leg2_filled = False
    leg1_order_id = None
    leg2_order_id = None

    while True:
        # CALCULATE REAL TIME REMAINING
        time_left = market_end_dt - datetime.now(timezone.utc)
        seconds_left = time_left.total_seconds()
        # Minutes elapsed since the start of a standard 15m window 
        # (Total 15 - remaining)
        current_minute = 15 - (seconds_left / 60)

        if seconds_left < 1:
            print(f"❌ Current market - {market_name} trading window is closed.")
            return
        # ---------------------------------------------
        # Check trading window is OPEN.
        # ---------------------------------------------
        if seconds_left <= ABORT_TRADE_WINDOW_MINUTE * 60 and not leg1_filled and not leg2_filled:
            print(f"❌ Trading window is below {ABORT_TRADE_WINDOW_MINUTE} minutes. Both Leg1 and Leg2 NOT Filled. Aborting Trade.")
            print()
            return
        
        # ---------------------------------------------
        # Kill Switch. Cut whatever win/losses since arbitrage is not successful
        # ---------------------------------------------
        if seconds_left <= KILL_SWITCH_MINUTE * 60:
            print(f"\n⛔ Warning: Only {seconds_left/60:.1f}m left in window.")
            # 2. Sell off Leg 1 if it was filled but Leg 2 wasn't (the "Arb" failed)
            if leg1_filled and not leg2_filled:
                # try:
                #     client.cancel_order(leg2_order_id)
                # except: pass
                try:
                    # Fetch the current best bid to ensure we fill immediately
                    # Note: We sell at a lower price just to exit (Market Sell)
                    exit_bid = get_live_bid(l1_tid)
                    sell_price = exit_bid if exit_bid else 0.05 # Panic price if bid is gone
                    print(f"🔥 KILL SWITCH - PANIC SELL: Liquidating {leg1_side} @ ${sell_price}")
                
                    sell_order = client.create_order(OrderArgs(
                        price=sell_price,
                        size=TARGET_USDC_PER_LEG,
                        side="SELL",
                        token_id=l1_tid
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

                # --- PHASE 1: SEARCHING FOR LEG 1 ---
                if not leg1_filled:
                    # We need both prices to verify the arbitrage exists right now
                    if bid_up and bid_down:
                        total_cost = bid_up + bid_down
                        
                        # Pick cheaper leg as L1
                        if bid_up <= bid_down:
                            l1_side, l1_tid, l1_price, l2_tid, l2_price = "Up", up_id, bid_up, down_id, bid_down
                        else:
                            l1_side, l1_tid, l1_price, l2_tid, l2_price = "Down", down_id, bid_down, up_id, bid_up

                        if l1_price <= ANCHOR_PRICE:
                            l1_size = round(TARGET_USDC_PER_LEG / l1_price, 2)

                            print(f"\n🚀 ENTRY TRIGGERED: {l1_side} @ ${l1_price} (Target <= ${ANCHOR_PRICE})")
                            print(f"\n🚀 ENTRY TRIGGERED: Buying {l1_size} {l1_side} @ ${l1_price} (Cost: ~${TARGET_USDC_PER_LEG})")

                            order = client.create_order(OrderArgs(
                                price=round(l1_price, 2), size=l1_size, side="BUY", token_id=l1_tid
                            ))

                            print(f"order: {order}")
                            print(f"📡 Dispatching Order to CLOB...")
                            resp = client.post_order(order)
                            if resp and resp.get("success"):
                                print("✅ Order Accepted!")
                                leg1_filled = True
                                # Save state for Hedge and Kill Switch
                                leg1_filled, leg1_side, leg1_tid, leg1_price = True, l1_side, l1_tid, l1_price
                            else:
                                print(f"❌ Leg1 Order Rejected: {resp.get('errorMsg')}")
                elif leg1_filled and not leg2_filled:
                        # --- CALCULATE LEG 2 (Hedge) ---
                        current_hedge_bid = bid_down if leg1_side == "Up" else bid_up
                        # Define your "Dream Price" for the hedge (e.g., 2 cents cheaper than current)
                        # target_hedge_price = round(TARGET_PAIR_COST - leg1_price, 2)
                        max_hedge_price = round(TARGET_PAIR_COST - leg1_price, 2)
                        dream_hedge_price = max_hedge_price - PATIENCE_BUFFER
                        strike_price = dream_hedge_price if current_minute < 10 else max_hedge_price
                        print(f"⏳ Waiting for Hedge! Current: {current_hedge_bid} | Target: {dream_hedge_price} | Strike Price: {strike_price}")

                        if current_hedge_bid and current_hedge_bid <= dream_hedge_price:
                            l2_size = round(TARGET_USDC_PER_LEG / current_hedge_bid, 2)
                            print(f"\n🎯 Target hit! Hedging @ {current_hedge_bid}")
                            h_resp = client.post_order(client.create_order(OrderArgs(
                                price=round(current_hedge_bid, 2),
                                size=l2_size,
                                side="BUY",
                                token_id=l2_tid
                            )))
                            if h_resp and h_resp.get("success"):
                                print("💰 ARB COMPLETE: Locked in extra alpha.")
                                leg2_filled = True
                            leg2_order_id = h_resp.get("orderID") if h_resp else None

                # --- LEG 2 FILL CHECK ---
                if leg1_filled and not leg2_filled and leg2_order_id:
                    order_status = client.get_order(leg2_order_id)
                    if utils.is_filled(order_status):
                        print("\n💰 ARB LOCKED! Both sides filled.")
                        leg2_filled = True
            except Exception as e:
                time.sleep(0.5) # Simple retry delay
                print(f"💥 Exception occurred: {e}")
        time.sleep(0.5)

def get_event_id():
    # parse event_url from CLI and extract the eventId
    parser = argparse.ArgumentParser()
    parser.add_argument("slug", help="event_url")
    args = parser.parse_args()
    return args.slug

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

if __name__ == "__main__":
    print("=== POLYMARKET 15M ARB BOT INITIALIZED ===")
    start_arb_loop()
