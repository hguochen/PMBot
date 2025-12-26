import ccxt
import os
import statistics
import time
import math
import utils


from collections import deque
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, MarketOrderArgs, OrderType, OrderArgs
from dotenv import load_dotenv

# =========================================================
# 0. LOGGING SETTINGS
# =========================================================
EVENTS_FILE = "events/btc_15m_events_12262025.txt"
LOG_FILE = "logs/btc_15m_events_12262025_logs.txt"

# =========================================================
# 1. HARDCODED TARGET SETTINGS
# =========================================================

# when time is less than value, abort trade
ABORT_TRADE_WINDOW_MINUTE = 3
ABORT_TRADE_WINDOW_SECONDS = ABORT_TRADE_WINDOW_MINUTE * 60
# open trading window
TRADE_WINDOW_MINUTE = 14
TRADE_WINDOW_SECONDS = 870
# time remaining to execute a hard exit, regardless of current active position win/lose
HARD_EXIT_SECONDS = 150

# amount of USDC to fill in each purchase
TARGET_USDC_SIZE = 5.00
# True if hard exit feature is enabled, False otherwise
HARD_EXIT_ENABLED = True
# lowest price to execute stop loss SELL order
HARD_STOP_LOSS_PRICE = 0.35
# preserve 60% of the capital in the event of a stop loss 
CAPITAL_PRESERVATION_RATIO = 0.60
# max slippage allowed
MAX_SLIPPAGE = 0.02
# price difference between buyer book and seller book
ASK_VS_BID_SPREAD = 0.04

# must be < 1 for a profit
TARGET_PAIR_COST = 0.90
# arbitrage pair cost
ARBITRAGE_PAIR_COST = 0.95
# minimum leg 1 bid cost price
MIN_LEG_1_COST_PRICE = 0.38
# maximum leg 1 cost price
MAX_LEG_1_COST_PRICE = 0.45
# minimum leg 2 bid cost price
MIN_LEG_2_COST_PRICE = 0.40
# maximum leg 2 bid cost price
MAX_LEG_2_COST_PRICE = 0.45

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
# 3. MAIN BOT
# =========================================================
def run_bot(event_url):
    try:
        # Get token details, name of current market and end time
        # sample token: {'Up': '30153181842485352904197017700459535515576600216562347098287653572010919110026', 'Down': '34727604537658227042683904687799441550554770516513901542888684177893436283551'}
        # sample market_name: Bitcoin Up or Down - December 21, 5:00AM-5:15AM ET
        # sample end time: 2025-12-21T10:15:00Z
        print(f"🤔 Fetching market data for event: {event_url}")
        print()
        tokens, market_name, end_time = utils.get_market_by_slug(utils.extract_slug(event_url))
        append_log([
            "",
            event_url,
            f"- Market: {market_name}"
        ])

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
        print(f"🟢 Market ACTIVE: {market_name}")
        print(f"🟢 Trading options -> UP: {up_id} | DOWN: {down_id}")
        print()

        # 0. Initialize variables
        # True if you currently have an active position in this market, False otherwise
        IN_POSITION = False
        # True if you can bid in current market, False otherwise
        SHOULD_BID = True
        # True if you currently have both active UP and DOWN positions, False otherwise
        IN_ARBITRAGE = False
        # Leg 1 UP/DOWN direction. None if there's no active market position
        LEG_1_BID_DIRECTION = None
        # Leg 2 UP/DOWN direction. None if there's no leg 2 market position
        LEG_2_BID_DIRECTION = None
        # leg 2 limit order ID. used if leg 1 gets stop loss executed
        ACTIVE_LIMIT_ORDER_ID = None
        
        # leg 1 bid price
        LEG_1_BID_PRICE = 0.00
        # leg 2 bid price
        LEG_2_BID_PRICE = 0.00
        # number of leg 1 shares
        LEG_1_SHARES = 0.00
        # number of leg 2 shares
        LEG_2_SHARES = 0.00
        # leg 1 stop loss price
        EFFECTIVE_STOP_LOSS = HARD_STOP_LOSS_PRICE
        # track the last 10 prices for the ACTIVE position to filter noise
        last_ten_up_bids = deque(maxlen = 10)
        last_ten_down_bids = deque(maxlen = 10)

        while True:
            # -----------------------------------------
            # 1. TIME CALCULATION
            # -----------------------------------------
            time_left = market_end_dt - datetime.now(timezone.utc)
            seconds_left = time_left.total_seconds()
            current_minute = 15 - (seconds_left / 60)
            if seconds_left < 1:
                print(f"❌ Current market - {market_name} trading window is closed.")
                return

            # -----------------------------------------
            # 2. FETCH PRICES
            # -----------------------------------------
            raw_up = get_live_ask(up_id)
            raw_down = get_live_ask(down_id)
            if raw_up is None or raw_down is None:
                print(f"⚠️ Skipping tick: API returned None for prices. UP: {raw_up} | DOWN: {raw_down}")
                continue
            bid_up = round(raw_up, 2)
            bid_down = round(raw_down, 2)
            # calculate the rolling mean price of up and down bid prices
            last_ten_up_bids.append(bid_up)
            last_ten_down_bids.append(bid_down)
            up_mean_price = bid_up
            down_mean_price = bid_down
            if (len(last_ten_up_bids) >=3 and len(last_ten_down_bids) >=3):
                up_mean_price = round(statistics.mean(last_ten_up_bids), 2)
                down_mean_price = round(statistics.mean(last_ten_down_bids), 2)

            if not isinstance(bid_up, (int, float)) or not isinstance(bid_down, (int, float)):
                print(f"Prices are not numbers: bid_up: {bid_up}, bid_down:{bid_down}")
                continue
            
            print(f"📈 Currently running market: {market_name} - {event_url}")
            print(f"📖 Time Left: {seconds_left//60:.0f}m {seconds_left%60:.0f}s | In Trade?: {IN_POSITION} | In Arbitrage? {IN_ARBITRAGE} | ⬆️ UP Bid: {bid_up or 'N/A'} | ⬇️ DOWN Bid: {bid_down or 'N/A'}")
            print(f"DEBUGGING: ACTIVE_LIMIT_ORDER_ID: {ACTIVE_LIMIT_ORDER_ID}")

            if SHOULD_BID and not IN_ARBITRAGE and seconds_left > ABORT_TRADE_WINDOW_SECONDS and seconds_left < TRADE_WINDOW_SECONDS:
                print(f"🟢 Trade window: OPEN")
            else:
                print(f"❌ Trade window: CLOSED")
            # -----------------------------------------
            # 3. BID STATUS UPDATE
            # -----------------------------------------
            if IN_POSITION and not IN_ARBITRAGE:
                print(f"You have an ACTIVE position. bid: {LEG_1_BID_DIRECTION} | shares_amount: {LEG_1_SHARES} | total_invested_size: {LEG_1_BID_PRICE * LEG_1_SHARES} | Effective stop loss price: {EFFECTIVE_STOP_LOSS}")

            # -----------------------------------------
            # 4. HARD TIME-BASED EXIT (Liquidity Guard)
            # by default, if leg 2 is not filled by 2min30s left, we will hard exit and execute stop loss
            # -----------------------------------------
            # only applies to directional trades. Arbitrage(BOTH) runs to expiry
            if IN_ARBITRAGE and seconds_left > 0:
                    print(f"📈💰 Congratulations! Dutch Book trade LOCKED. No Loss trade achieved!")
                    print(f"💰 Leg 1: {LEG_1_BID_DIRECTION} | Leg 1 Shares: {LEG_1_SHARES} | Leg 1 Price: {LEG_1_BID_PRICE} ")
                    print(f"💰 Leg 2: {LEG_2_BID_DIRECTION} | Leg 2 Shares: {LEG_2_SHARES} | Leg 2 Price: {LEG_2_BID_PRICE} ")
                    # arbitrage locked. we don't need to spam the service anymore. rest for 5 seconds before checking time again
                    time.sleep(5)
                    continue
            elif should_hard_exit_market(IN_POSITION, IN_ARBITRAGE, seconds_left):
                append_log([
                    f"⏰ HARD EXIT TRIGGERED. {seconds_left:.0f}s left. Closing to avoid liquidity trap."
                ])
                print(f"⏰ HARD EXIT TRIGGERED. {seconds_left:.0f}s left. Closing to avoid liquidity trap.")
                if ACTIVE_LIMIT_ORDER_ID:
                    print(f"🧹 Cleaning up pending Leg 2 Limit Order: {ACTIVE_LIMIT_ORDER_ID}")
                    limit_order_cancel_response = client.cancel(order_id = ACTIVE_LIMIT_ORDER_ID)
                    # Check if order was successfully canceled
                    if limit_order_cancel_response.get("canceled") and ACTIVE_LIMIT_ORDER_ID in limit_order_cancel_response["canceled"]:
                        print(f"🧹 Limit Order: {ACTIVE_LIMIT_ORDER_ID} successfully cancelled.")
                        ACTIVE_LIMIT_ORDER_ID = None
                    elif limit_order_cancel_response.get("not_canceled") and ACTIVE_LIMIT_ORDER_ID in limit_order_cancel_response["not_canceled"]:
                        reason = limit_order_cancel_response["not_canceled"][ACTIVE_LIMIT_ORDER_ID]
                        print(f"❌ Limit Order: {ACTIVE_LIMIT_ORDER_ID} failed to cancel: {reason}")
                
                exit_id = up_id if LEG_1_BID_DIRECTION == "UP" else down_id
                try:
                    hard_exit_response = execute_stop_loss(exit_id, LEG_1_SHARES, EFFECTIVE_STOP_LOSS, "SELL", MAX_SLIPPAGE)
                    if hard_exit_response and hard_exit_response.get("success"):
                        IN_POSITION = False
                        SHOULD_BID = False
                        append_log([f"🟢 HARD TIME EXIT SUCCESSFUL @ {seconds_left:.0f}s left."])
                        print(f"🟢 HARD TIME EXIT SUCCESSFUL @ {seconds_left:.0f}s left.")
                        continue
                    else:
                        print(f"❌ Hard Exit not successful! Execute stop loss response: {hard_exit_response}")
                except Exception as e:
                    append_log([
                        f"❌ Hard Exit Error: {e}"
                    ])
                    print(f"❌ Hard Exit Error: {e}")

            # -----------------------------------------
            # 5. STOP LOSS
            # -----------------------------------------
            if IN_POSITION and not IN_ARBITRAGE:
                # 5.1 Identify which price to track based on our current position
                current_price = bid_up if LEG_1_BID_DIRECTION == "UP" else bid_down

                # 5.2 Check if price has dropped to our floor
                if current_price <= EFFECTIVE_STOP_LOSS:
                    print(f"🚨 STOP LOSS TRIGGERED! Price {current_price} <= {EFFECTIVE_STOP_LOSS}")
                    append_log([
                        f"🚨 STOP LOSS TRIGGERED! Price {current_price} <= {EFFECTIVE_STOP_LOSS}",
                        f"Leg 2 Limit Order ID: {ACTIVE_LIMIT_ORDER_ID}"
                    ])
                    # cancel the pending leg 2 limit order immediately
                    try:
                        if ACTIVE_LIMIT_ORDER_ID:
                            print(f"🧹 Cleaning up pending Leg 2 Limit Order: {ACTIVE_LIMIT_ORDER_ID}")
                            limit_order_cancel_response = client.cancel(order_id = ACTIVE_LIMIT_ORDER_ID)
                            # Check if order was successfully canceled
                            if limit_order_cancel_response.get("canceled") and ACTIVE_LIMIT_ORDER_ID in limit_order_cancel_response["canceled"]:
                                print(f"🧹 Limit Order: {ACTIVE_LIMIT_ORDER_ID} successfully cancelled.")
                                ACTIVE_LIMIT_ORDER_ID = None
                            elif limit_order_cancel_response.get("not_canceled") and ACTIVE_LIMIT_ORDER_ID in limit_order_cancel_response["not_canceled"]:
                                reason = limit_order_cancel_response["not_canceled"][ACTIVE_LIMIT_ORDER_ID]
                                print(f"❌ Limit Order: {ACTIVE_LIMIT_ORDER_ID} failed to cancel: {reason}")
                        else:
                            print(f"Leg 2 Limit Order not found. ACTIVE_LIMIT_ORDER_ID: {ACTIVE_LIMIT_ORDER_ID}")
                    except Exception as e:
                        append_log([
                            f"Error trying to cancel Leg 2 Limit order: {e}"
                        ])
                        print(f"Error trying to cancel Leg 2 Limit order: {e}")

                    # 5.3 Liquidate: Sell the directional leg 1 position
                    exit_id = up_id if LEG_1_BID_DIRECTION == "UP" else down_id
                    try:
                        print(f"🔥 Panic Selling {LEG_1_BID_DIRECTION} to preserve capital...")
                        sell_resp = execute_stop_loss(exit_id, LEG_1_SHARES, EFFECTIVE_STOP_LOSS, "SELL", MAX_SLIPPAGE)
                        if sell_resp and sell_resp.get("success"):
                            append_log([f"🔴 STOP LOSS EXECUTED @ {current_price} in {market_name}"])
                            print(f"🟢 LIQUIDATION SUCCESSFUL. Resetting bot for next event.")

                            IN_POSITION = False
                            SHOULD_BID = False
                            LEG_1_BID_DIRECTION = None
                            continue
                    except Exception as e:
                        print(f"❌ CRITICAL: Stop loss execution failed! Manual intervention may be needed: {e}")

            # -----------------------------------------
            # 6. INSTANT ARBITRAGE
            # -----------------------------------------
            if SHOULD_BID and not IN_POSITION and not IN_ARBITRAGE and seconds_left > ABORT_TRADE_WINDOW_SECONDS:
                # CHECK ARBITRAGE MATH FIRST
                # We look for a total pair cost below your ARBITRAGE_PAIR_COST
                combined_cost = bid_up + bid_down
                if (combined_cost <= ARBITRAGE_PAIR_COST):
                    print(f"💰 ARB FOUND! UP Cost: {bid_up} | DOWN Cost: {bid_down}")

                    # pick the cheaper leg to start with (Leg 1)
                    target_id = up_id if bid_up <= bid_down else down_id
                    target_price = bid_up if bid_up <= bid_down else bid_down
                    target_direction = "UP" if bid_up <= bid_down else "DOWN"

                    print(f"⚡ Arbitrage entering Leg 1 BUY {target_direction} @ Price: {target_price}")
                    try:
                        # execute market order for leg 1
                        leg_1_response, leg_1_shares = execute_market_buy(target_id, target_price, OrderType.FOK, "BUY", MAX_SLIPPAGE)
                        if leg_1_response and leg_1_response.get("success"):
                            IN_POSITION = True
                            LEG_1_BID_DIRECTION, LEG_1_BID_PRICE, LEG_1_SHARES = target_direction, target_price, round(leg_1_shares, 2)

                            # immediately attempt leg 2
                            other_id = down_id if target_direction == "UP" else up_id
                            other_price = bid_down if target_direction == "UP" else bid_up
                            other_direction = "DOWN" if target_direction == "UP" else "UP"

                            print(f"⚡ HEDGING: Attempting Leg 2 @ {other_price}")
                            # buy leg 2 without looking at slippage, we try our best to succeed here
                            leg_2_response, leg_2_shares = execute_market_buy(other_id, other_price, OrderType.FOK, "BUY", MAX_SLIPPAGE + 0.02)
                            if leg_2_response and leg_2_response.get("success"):
                                IN_ARBITRAGE = True
                                LEG_2_BID_DIRECTION, LEG_2_BID_PRICE, LEG_2_SHARES = other_direction, other_price, round(leg_2_shares, 2)
                                print(f"💰 Congratulations! Dutch Book trade LOCKED. No Loss trade achieved!")
                                print(f"💰 Leg 1: {LEG_1_BID_DIRECTION} | Leg 1 Shares: {LEG_1_SHARES} | Leg 1 Price: {LEG_1_BID_PRICE} ")
                                print(f"💰 Leg 2: {LEG_2_BID_DIRECTION} | Leg 2 Shares: {LEG_2_SHARES} | Leg 2 Price: {LEG_2_BID_PRICE} ")
                                # arbitrage locked. we don't need to spam the service anymore. rest for 5 seconds before checking time again
                                time.sleep(5)
                    except Exception as e:
                        print(f"❌ Arbitrage failed. Error: {e}")

            # -----------------------------------------
            # 6. MONITOR FOR FILL (Check if Leg 2 is now in wallet)
            # -----------------------------------------
            if IN_POSITION and not IN_ARBITRAGE:
                print(f"🛰️ Checking if Leg 2 is now filled...")
                # Check the balance of the other token
                other_id = down_id if LEG_1_BID_DIRECTION == "UP" else up_id
                balance_info = client.get_balance_allowance(BalanceAllowanceParams(
                    asset_type = AssetType.CONDITIONAL,
                    token_id = other_id
                ))

                # if balance > 0, the limit order was hit!
                leg_2_shares = float(balance_info.get("balance", 0)) / 1e6
                if leg_2_shares > 0.1:
                    append_log([
                        f"📈💰 ARBITRAGE ACTIVE: Leg 2 Limit Order Filled!",
                        f"📈💰 Congratulations! Dutch Book trade LOCKED. No Loss trade achieved!",
                        f"💰 Leg 1: {LEG_1_BID_DIRECTION} | Leg 1 Shares: {LEG_1_SHARES} | Leg 1 Price: {LEG_1_BID_PRICE} ",
                        f"💰 Leg 2: {LEG_2_BID_DIRECTION} | Leg 2 Shares: {LEG_2_SHARES} | Leg 2 Price: {LEG_2_BID_PRICE} "
                    ])
                    print(f"💰 ARBITRAGE ACTIVE: Leg 2 Limit Order Filled!")
                    IN_ARBITRAGE = True
                    LEG_2_SHARES = leg_2_shares
                    LEG_2_BID_DIRECTION = "DOWN" if LEG_1_BID_DIRECTION == "UP" else "UP"
                else:
                    print(f"❌ ARBITRAGE INACTIVE: Leg 2 Limit Order NOT filled Yet!")

            # -----------------------------------------
            # 7. DIRECTIONAL MOINTORING & LEG-IN 
            # -----------------------------------------
            if should_monitor_bids(SHOULD_BID, IN_POSITION, seconds_left):
                print(f"👀 Monitoring ACTIVE. Looking for Leg 1 BIDS above {MIN_LEG_1_COST_PRICE} and below {MAX_LEG_1_COST_PRICE} | UP Mean: {up_mean_price:.3f} | DOWN Mean: {down_mean_price:.3f}")
                # Check for Leg 1 entry
                dist_up = abs(bid_up - MIN_LEG_1_COST_PRICE)
                dist_down = abs(bid_down - MIN_LEG_1_COST_PRICE)
                target_side = None
                # buy UP/DOWN is eligible iff:
                # - Price is more than MIN_LEG_1_COST_PRICE
                # - Price is less than MAX_LEG_1_COST_PRICE
                # - Price is on a trending momentum by being higher than the last 10 prices
                # - Price is the lesser amount between up and down
                up_eligible = bid_up >= MIN_LEG_1_COST_PRICE and bid_up <= MAX_LEG_1_COST_PRICE and bid_up >= up_mean_price and bid_up <= bid_down
                down_eligible = bid_down >= MIN_LEG_1_COST_PRICE and bid_down <= MAX_LEG_1_COST_PRICE and bid_down >= down_mean_price and bid_down < bid_up

                # decision matrix to buy UP/DOWN
                if up_eligible and down_eligible:
                    # if both hit target, pick the one closer to the target(the "purer" entry)
                    target_side = "UP" if dist_up <= dist_down else "DOWN"
                elif up_eligible:
                    target_side = "UP"
                elif down_eligible:
                    target_side = "DOWN"
                else:
                    print(f" Both UP/DOWN prices are not eligible for purchase. UP: {bid_up} | DOWN: {bid_down}")
                    continue
                target_id = up_id if target_side == "UP" else down_id
                target_price = bid_up if target_side == "UP" else bid_down
                print(f"Buy Decision matrix result: UP eligible: {up_eligible} | DOWN eligible: {down_eligible} | BUY direction: {target_side} | BUY price: {target_price}")
                
                print(f"⚡ Directional entering Leg 1 BUY {target_side} @ Price: {target_price}")
                try:
                    # execute market order for leg 1
                    leg_1_response, leg_1_shares = execute_market_buy(target_id, target_price, OrderType.FOK, "BUY", MAX_SLIPPAGE)
                    if leg_1_response:
                        if not leg_1_response.get("success"):
                            append_log([
                                f"💀 Leg 1 Market order failed. Error: {leg_1_response.get('errorMsg')}"
                            ])
                            print(f"💀 Leg 1 Market order failed. Error: {leg_1_response.get('errorMsg')}")
                            print(f" Skipping Leg 1 BUY...")
                            continue
                        IN_POSITION = True
                        SHOULD_BID = False
                        LEG_1_BID_DIRECTION, LEG_1_BID_PRICE, LEG_1_SHARES = target_side, target_price, round(leg_1_shares, 2)
                        EFFECTIVE_STOP_LOSS = round(LEG_1_BID_PRICE * CAPITAL_PRESERVATION_RATIO, 2)
                        append_log([
                            f"✅ BUY UP @ {round(LEG_1_BID_PRICE, 2)} on shares: {round(LEG_1_SHARES, 2)}. total size: {LEG_1_SHARES * LEG_1_BID_PRICE}"
                        ])
                        print(f"✅📈 You have successfully placed a buy {LEG_1_BID_DIRECTION}. Good Luck!")
                        print(f"🟢 Purchase complete! Purchase Details:")
                        print(f"🟢 target_id: {target_id}")
                        print(f"🟢 Leg 1 bid direction: {LEG_1_BID_DIRECTION}")
                        print(f"🟢 Leg 1 price: {round(LEG_1_BID_PRICE, 2)}")
                        print(f"🟢 Leg 1 shares count: {LEG_1_SHARES}")

                        # PLACE LEG 2 LIMIT ORDER IMMEDIATELY
                        other_id = down_id if target_side == "UP" else up_id
                        # limit_price = round(TARGET_PAIR_COST - LEG_1_BID_PRICE, 2)
                        # we use MAX_LEG_2_COST_PRICE to maximize our chances of filling leg 2, sacrificing the chance of buying in at a lesser price and therefore more profits
                        limit_price = TARGET_PAIR_COST - LEG_1_BID_PRICE
                        LEG_2_BID_PRICE = limit_price
                        
                        print(f"📡 Posting Leg 2 LIMIT BUY Order @ {limit_price}...")
                        try:
                            # Create a Limit Order (GTC)
                            # Note: Limit orders usually require a specific 'price' and 'token_amount'
                            limit_order = client.create_order(OrderArgs(
                                price = limit_price, # Your target Leg 2 price
                                size = LEG_1_SHARES, # The number of SHARES (not USDC amount)
                                side = "BUY",
                                token_id = other_id
                            ))
                            limit_resp = client.post_order(limit_order, OrderType.GTC)

                            if limit_resp and limit_resp.get("success"):
                                print(f"✅ Leg 2 Limit Order LIVE on book. ID: {limit_resp.get('orderID')}")

                                order_id = limit_resp.get("orderID")
                                if order_id:
                                    ACTIVE_LIMIT_ORDER_ID = order_id # This sets the global-loop variable
                                    print(f"✅ Leg 2 Limit Order LIVE on book. ID: {ACTIVE_LIMIT_ORDER_ID}")
                                    append_log([f"🟢 Leg 2 LIMIT order ID: {ACTIVE_LIMIT_ORDER_ID}"])
                                else:
                                    print("⚠️ Order success, but could not find orderID key in response JSON.")
                            else:
                                print(f"💀 Leg 2 LIMIT order failed. Error: {limit_resp.get('errorMsg')}")
                        except Exception as e:
                            append_log([
                                f"💀 Failed to post Limit Order: {e}"
                            ])
                            print(f"💀 Failed to post Limit Order: {e}")
                    else:
                        print(f"Buy {target_side} not successful!")
                except Exception as e:
                    append_log([
                        f"💀 Leg 1 BUY {target_side} Failed. {target_side} price: {target_price} | Error: {e}"
                    ])
                    print(f"💀 Leg 1 BUY {target_side} Failed. {target_side} price: {target_price} | Error: {e}")
            time.sleep(0.2)
    except Exception as e:
        append_log([
            f"Error occurred running event {market_name}. Error: {e}"
        ])
        print(f"Error occurred running event {market_name}. Error: {e}")
    return

def run_events():
    events = utils.read_polymarket_events(EVENTS_FILE)
    if not events:
        print(f"❌ No events found in {EVENTS_FILE}")
        return

    for url, name in events:
        print("\n" + "=" * 60)
        print(f"🎯 Processing event: {name}")
        print(f"🎯 Event URL: {url}")
        try:
            run_bot(url)
            append_log([
                f"🟢 Run successful - {url}"
            ])
        except Exception as e:
            append_log([
                f"❌ Exception occurred running {url}: {e}"
            ])
        print("=" * 60)
    print(f"All events in {EVENTS_FILE} ran successfully")
    return

# =========================================================
# 3. Utility Methods
# =========================================================
def should_execute_stop_loss(current_price, effective_stop_loss):
    return current_price <= effective_stop_loss

def should_hard_exit_market(in_position, in_arbitrage, seconds_left):
    return HARD_EXIT_ENABLED and in_position and not in_arbitrage and seconds_left <= HARD_EXIT_SECONDS

def should_monitor_bids(should_bid, in_position, seconds_left):
    return should_bid and not in_position and seconds_left < TRADE_WINDOW_SECONDS and seconds_left >= ABORT_TRADE_WINDOW_SECONDS

def execute_market_buy(token_id, target_price, order_type, side="BUY", max_slippage = 0.02):
    """
    OrderType.FOK = Fill-or-Kill
    OrderType.FAK = Fill-and-Kill
    OrderType.GTC = Limit order
    """
    # 1. Quick refresh: get the latest price
    current_ask = get_live_ask(token_id)
    current_bid = get_live_bid(token_id)

    # 2. Spread check
    if current_ask and current_bid:
        spread = current_ask - current_bid
        if spread > ASK_VS_BID_SPREAD: # If spread wide, it's too expensive to enter
            print(f"⚠️ Spread too wide ({round(spread, 3)}). Aborting entry.")
            return None, 0
    if not current_ask:
        print(f"⚠️ Skipping BUY: API returned None for prices. bid response: {current_ask}")
        return None, None

    # 3. Safety Check: If price jumped too high(for BUY)
    if (side == "BUY" and current_ask > (target_price + max_slippage)):
        print(f"⚠️ Slippage Guard: Aborting. Price jumped from {target_price} to {current_ask}")
        return None, 0
    
    # Create and post order
    shares = round(float(TARGET_USDC_SIZE) / current_ask, 2)
    print(f"-------------------Attempting to BUY-----------------------")
    print(f"BUY price: {round(current_ask, 2)}")
    print(f"BUY amount: {TARGET_USDC_SIZE}")
    print(f"-------------------Attempting to BUY-----------------------")
    order = client.create_market_order(MarketOrderArgs(
        price = round(current_ask, 2),
        amount = TARGET_USDC_SIZE,
        side = side,
        token_id = token_id
    ))
    return client.post_order(order, order_type), shares


def execute_stop_loss(token_id, shares, stop_loss_price, side="SELL", max_slippage = 0.02):
    # 1. Repeatedly submits FAK(Fill-and-Kill) orders until the conditional token balance for the specific market is 0
    response = None
    while True:
        # 1. Fetch live on-chain balance
        balance_info = client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id
            )
        )
        balance = float(balance_info.get("balance", 0)) / 1e6

        if balance < 0.01:
            print(f"✅ Liquidation complete. Final Balance: {balance}")
            return response

        # 2. Quick refresh: Get the very latest price
        current_bid = get_live_bid(token_id)
        if not current_bid:
            print("⚠️ No liquidity found. Waiting to retry...")
            continue
        print(f"📘 Stop loss price: {current_bid}, Effective stop loss price: {stop_loss_price}")
        if current_bid > stop_loss_price:
            print(f"🚀💥 Aborting Stop Loss Execution. Current price: {current_bid} has recovered from Stop loss price: {stop_loss_price}")
            break
        # Market SELL: amount = number of shares. 
        # API Rule: Sell orders must have max 2 decimal places for the share amount.
        sell_qty = math.floor(balance * 100) / 100.0
        print(f"-------------------Attempting STOP LOSS SELL -----------------------")
        print(f"Sell Type: Fill-and-Kill")
        print(f"Wallet Balance: {balance}")
        print(f"Targeting Bid: {current_bid} | Selling Qty: {sell_qty}")
        print(f"-------------------Attempting STOP LOSS SELL -----------------------")

        # 3. execute the stop loss
        try:
            order = client.create_market_order(MarketOrderArgs(
                amount = round(sell_qty, 2), # number of shares to sell
                side = side,
                token_id = token_id
            ))
            response = client.post_order(order, OrderType.FAK)
            if response and response.get("success"):
                print(f"🟢 Successfully processed partial/full fill.")
            else:
                print(f"❌ Stop Loss Sell rejected or expired. Retrying...")
        except Exception as e:
            print(f"❌ Stop Loss execution Error: {e}. Retrying...")
        time.sleep(0.2)
    return response

def get_live_ask(token_id):
    """
    Price to BUY from (Sellers)
    """
    try:
        response = client.get_price(token_id, side="SELL")
        return float(response.get("price")) if response else None
    except:
        return None

def get_live_bid(token_id):
    """
    Price to SELL into (Buyers)
    """
    try:
        response = client.get_price(token_id, side="BUY")
        return float(response.get("price")) if response else None
    except:
        return None

def append_log(lines):
    """
    Appends a list of lines to the trade log file.
    """
    with open(LOG_FILE, "a") as f:
        for line in lines:
            f.write(line + "\n")

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🤖🤖🤖 Dutch Book BOT INITIALIZED 🤖🤖🤖")
    print("\n" + "=" * 60)
    run_events()
