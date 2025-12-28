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
EVENTS_FILE = "events/btc_15m_events_12282025.txt"
LOG_FILE = "logs/btc_15m_events_12282025_logs.txt"

# =========================================================
# 1. HARDCODED TARGET SETTINGS
# =========================================================
# when time is less than value, abort trade
ABORT_TRADE_WINDOW_MINUTE = 5
ABORT_TRADE_WINDOW_SECONDS = ABORT_TRADE_WINDOW_MINUTE * 60
# open trading window
TRADE_WINDOW_MINUTE = 14
TRADE_WINDOW_SECONDS = TRADE_WINDOW_MINUTE * 60 + 50
# time remaining to execute a hard exit, regardless of current active position win/lose. 2min
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
TARGET_PAIR_COST = 0.92
# arbitrage pair cost
ARBITRAGE_PAIR_COST = 0.95
# minimum leg 1 bid cost price
MIN_LEG_1_COST_PRICE = 0.41
# maximum leg 1 cost price
MAX_LEG_1_COST_PRICE = 0.48
# if take profit is enabled, when price ratio reaches TAKE_PROFIT_RATIO, will sell to TP
TAKE_PROFIT_ENABLED = False
# gain percentage to trigger an immediate exit (e.g., 0.20 = 20% profit)
TAKE_PROFIT_RATIO = 0.75

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
            f"\n{'='*60}",
            f"🚀 STARTING EVENT: {market_name}",
            f"🔗 URL: {event_url}",
            f"⏰ Market End: {end_time}",
            f"{'='*60}"
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

        # -----------------------------------------
        # 0. INITIALIZE VARIABLES
        # -----------------------------------------
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
                log_finish = f"🏁 Market {market_name} closed. Exiting loop."
                print(log_finish)
                append_log([log_finish])
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

            if SHOULD_BID and seconds_left > ABORT_TRADE_WINDOW_SECONDS and seconds_left < TRADE_WINDOW_SECONDS:
                print(f"🟢 Trade window: OPEN")
            else:
                print(f"❌ Trade window: CLOSED")

            # -----------------------------------------
            # 3. BID STATUS UPDATE
            # -----------------------------------------
            if IN_POSITION:
                print(f"You have an ACTIVE position. bid: {LEG_1_BID_DIRECTION} | shares_amount: {LEG_1_SHARES} | total_invested_size: {LEG_1_BID_PRICE * LEG_1_SHARES} | Effective stop loss price: {EFFECTIVE_STOP_LOSS}")
            
            # -----------------------------------------
            # 4. HARD TIME-BASED EXIT (Liquidity Guard)
            # by default, if BUY is not sold by 2min30s left, we will hard exit and execute stop loss
            # -----------------------------------------
            if should_hard_exit_market(IN_POSITION, seconds_left):
                msg = f"⏰ HARD EXIT TRIGGERED @ {seconds_left:.1f}s left."
                print(msg)
                exit_id = up_id if LEG_1_BID_DIRECTION == "UP" else down_id
                try:
                    hard_exit_response = execute_stop_loss(exit_id, LEG_1_SHARES, EFFECTIVE_STOP_LOSS, "SELL", MAX_SLIPPAGE)
                    if hard_exit_response and hard_exit_response.get("success"):
                        IN_POSITION = False
                        SHOULD_BID = False
                        append_log([f"✅ HARD EXIT SUCCESSFUL. Sold {LEG_1_SHARES} shares."])
                        print(f"🟢 HARD TIME EXIT SUCCESSFUL @ {seconds_left:.0f}s left.")
                        return
                    else:
                        msg_exit_failed = f"❌ Hard Exit not successful! Execute stop loss response: {hard_exit_response}"
                        print(msg_exit_failed)
                        append_log([msg_exit_failed])
                except Exception as e:
                    append_log([
                        f"❌ Hard Exit Error: {e}"
                    ])
                    print(f"❌ Hard Exit Error: {e}")

            # -----------------------------------------
            # 5. STOP LOSS
            # -----------------------------------------
            if IN_POSITION:
                # 5.1 Identify which price to track based on our current position
                current_price = bid_up if LEG_1_BID_DIRECTION == "UP" else bid_down

                # 5.2 Check if price has dropped to our floor
                if current_price <= EFFECTIVE_STOP_LOSS:
                    msg = f"🚨 STOP LOSS HIT: Price {current_price} <= {effective_stop_loss}"
                    print(msg)
                    append_log([msg])
                    # 5.3 Liquidate: Sell the directional leg 1 position
                    exit_id = up_id if LEG_1_BID_DIRECTION == "UP" else down_id
                    try:
                        print(f"🔥 Panic Selling {LEG_1_BID_DIRECTION} to preserve capital...")
                        sell_resp = execute_stop_loss(exit_id, LEG_1_SHARES, EFFECTIVE_STOP_LOSS, "SELL", MAX_SLIPPAGE)
                        if sell_resp and sell_resp.get("success"):
                            msg_liquidation = f"🔴 LIQUIDATION COMPLETE @ {current_price}"
                            print(msg_liquidation)
                            append_log([msg_liquidation])
                            IN_POSITION = False
                            SHOULD_BID = False
                            LEG_1_BID_DIRECTION = None
                            return
                    except Exception as e:
                        msg_exception = f"❌ CRITICAL: Stop loss execution failed! Manual intervention may be needed: {e}"
                        append_log([msg_exception])
                        print(msg_exception)

            # -----------------------------------------
            # 6. TAKE PROFIT LOGIC
            # -----------------------------------------
            if IN_POSITION and TAKE_PROFIT_ENABLED:
                # Check price of our current holding
                current_price = bid_up if LEG_1_BID_DIRECTION == "UP" else bid_down
                
                # Calculate target (e.g., Buy at 0.45 * 1.25 = 0.56)
                tp_price = round(LEG_1_BID_PRICE * (1 + TAKE_PROFIT_RATIO), 2)

                if current_price >= tp_price:
                    msg = f"💰 TAKE PROFIT HIT: Price {current_price} >= {tp_price}"
                    print(msg)
                    append_log([
                        msg,
                        f"💰 Take Profit Attempt Executed @ {current_price} (+{TAKE_PROFIT_RATIO * 100}%)"
                    ])

                    exit_id = up_id if LEG_1_BID_DIRECTION == "UP" else down_id
                    # liquidate position to take profits
                    try:
                        tp_response = execute_take_profit(exit_id, LEG_1_SHARES, tp_price, "SELL")
                        if tp_response and tp_response.get("success"):
                            msg_profit = f"🟢💰 Take Profit SUCCESSFUL! Executed @ {current_price} (+{TAKE_PROFIT_RATIO * 100}%) Resetting bot for next event."
                            append_log([msg_profit])
                            print(msg_profit)
                            IN_POSITION = False
                            SHOULD_BID = False
                            break
                    except Exception as e:
                        print(f"❌ Take Profit Execution Error: {e}")

            # -----------------------------------------
            # 7. DIRECTIONAL MONITORING & LEG-IN 
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
                # up_eligible = bid_up >= MIN_LEG_1_COST_PRICE and bid_up <= MAX_LEG_1_COST_PRICE and bid_up >= up_mean_price and bid_up <= bid_down
                # down_eligible = bid_down >= MIN_LEG_1_COST_PRICE and bid_down <= MAX_LEG_1_COST_PRICE and bid_down >= down_mean_price and bid_down < bid_up
                up_eligible = bid_up >= MIN_LEG_1_COST_PRICE and bid_up <= MAX_LEG_1_COST_PRICE and bid_up >= up_mean_price
                down_eligible = bid_down >= MIN_LEG_1_COST_PRICE and bid_down <= MAX_LEG_1_COST_PRICE and bid_down >= down_mean_price

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
                        entry_log = [
                            f"✅ POSITION ENTERED",
                            f"   - Side: {LEG_1_BID_DIRECTION}",
                            f"   - Avg Price: {LEG_1_BID_PRICE}",
                            f"   - Shares: {LEG_1_SHARES}",
                            f"   - Total Cost: ${round(LEG_1_SHARES * LEG_1_BID_PRICE, 2)}",
                            f"   - UP mean price: {up_mean_price}",
                            f".  - Last 10 UP bids: {last_ten_up_bids}"
                            f"   - DOWN mean price: {down_mean_price}",
                            f".  - Last 10 DOWN bids: {last_ten_down_bids}"
                        ]
                        append_log(entry_log)
                        print(f"✅📈 You have successfully placed a buy {LEG_1_BID_DIRECTION}. Good Luck!")
                        print(f"🟢 Purchase complete! Purchase Details:")
                        print(f"🟢 target_id: {target_id}")
                        print(f"🟢 Leg 1 bid direction: {LEG_1_BID_DIRECTION}")
                        print(f"🟢 Leg 1 price: {round(LEG_1_BID_PRICE, 2)}")
                        print(f"🟢 Leg 1 shares count: {LEG_1_SHARES}")
                    else:
                        print(f"Buy {target_side} not successful!")
                        append_log([f"Buy {target_side} not successful at price: {LEG_1_BID_PRICE}"])
                except Exception as e:
                    msg_exception = f"💀 Leg 1 BUY {target_side} Failed. {target_side} price: {target_price} | Error: {e}"
                    append_log([msg_exception])
                    print(msg_exception)
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n🛑 Program interrupted by user. Exiting...")
        return
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
# 4. Utility Methods
# =========================================================
def should_execute_stop_loss(current_price, effective_stop_loss):
    return current_price <= effective_stop_loss

def should_hard_exit_market(in_position, seconds_left):
    return HARD_EXIT_ENABLED and in_position and seconds_left <= HARD_EXIT_SECONDS

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

def execute_take_profit(token_id, shares, take_profit_price, side = "SELL"):
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

        # 1. Quick refresh: Get the very latest price
        current_bid = get_live_bid(token_id)
        if not current_bid:
            print("⚠️ No liquidity found. Waiting to retry...")
            continue
        print(f"📘 Take profit price: {current_bid}, Effective Take profit price: {take_profit_price}")
        if current_bid > take_profit_price:
            print(f"🚀💥 Aborting Take profit Execution. Current price: {current_bid} has recovered from Take profit price: {take_profit_price}")
            break
        # Market SELL: amount = number of shares. 
        # API Rule: Sell orders must have max 2 decimal places for the share amount.
        sell_qty = math.floor(balance * 100) / 100.0
        print(f"-------------------Attempting STOP LOSS SELL -----------------------")
        print(f"Sell Type: Fill-and-Kill")
        print(f"Wallet Balance: {balance}")
        print(f"Targeting Bid: {current_bid} | Selling Qty: {sell_qty}")
        print(f"-------------------Attempting STOP LOSS SELL -----------------------")

        # 3. execute the take profit
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
                print(f"❌ Take profit SELL rejected or expired. Retrying...")
        except Exception as e:
            print(f"❌ Take profit execution Error: {e}. Retrying...")
        time.sleep(0.2)
    return response

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
    print("🤖🤖🤖 Directional Trade BOT INITIALIZED 🤖🤖🤖")
    print("\n" + "=" * 60)
    run_events()
