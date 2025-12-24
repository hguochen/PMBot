import os
import time
import datetime
import math
import statistics

from collections import deque
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv
from types import SimpleNamespace
import ccxt
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderType, MarketOrderArgs, BalanceAllowanceParams, AssetType
from datetime import datetime, timezone
import utils
import argparse

"""
BTC 15m "Arb-Sniper" Bot Strategy Documentation

1. Core Strategy Overview
The bot operates on a dual-logic framework. It first scans for mathematical arbitrage opportunities (guaranteed profit) and, if none are found,
falls back to a directional momentum "sniper" strategy that bets on high-probability outcomes.

A. The Dutch Book (Arbitrage) EngineThis is the bot's primary goal. It exploits market mispricings where the total cost of buying both "YES"
and "NO" (Up and Down) tokens is less than the guaranteed $1.00 payout.The Math: bid_up + bid_down <= ARBITRAGE_THRESHOLD (0.94).The Logic: If
the combined price is $0.94$, the bot buys both sides. Since one side must resolve to $1.00$, the bot locks in a $0.06 (6.38%)$ profit regardless
of Bitcoin's price movement.Execution Priority: It uses an "Asymmetric Entry," buying the more expensive (trending) side first to capture the
price before it moves higher.B. The Directional "Sniper" EngineIf no arbitrage is available, the bot scans for high-certainty directional moves
during a specific time window.Target Entry: Only executes when an outcome is priced at $0.82$ or higher (TARGET_COST).The Logic: This bets on
"momentum follow-through"—the idea that if an outcome is already 82% likely, it is highly probable to finish at 100%.

2. Execution & Risk ManagementTo protect the P/L and prevent the "one big loss wipes out ten wins" scenario, the bot uses several advanced
safety layers
Slippage Guard - MAX_SLIPPAGE (0.02) - Refreshes the price milliseconds before buying. Aborts if the price jumps >$0.02 to avoid "buying the top."
Stop-Loss - STOP_LOSS_THRESHOLD (0.65) - Automatically liquidates directional positions if the price drops below $0.65 to prevent a total 100% loss.
Time Window - 11m to 4m left - Only enters trades when enough time remains for the move to settle, but exits before the "final 4m" volatility crush.
Size Control - TARGET_USDC_SIZE (5.00) - Keeps every trade at a fixed $5.00 limit to ensure consistent position sizing and capital preservation.

3. Technical Workflow
Market Discovery: Fetches active 15m BTC market slugs from a local events.txt file.

Continuous Polling: Uses a while True loop with a 0.5s sleep timer to monitor the order book for both "UP" and "DOWN" tokens.

Validation: Ensures all bid data is numeric and non-null before attempting calculations to avoid script crashes.

Order Placement: Uses the py-clob-client to build and sign limit orders locally before posting them to the Polygon blockchain.

Logging: Every action (Buy, Sell, Stop-Loss, Arb-Entry) is recorded in a timestamped log file for P/L auditing.

4. Profitability Analysis (Current Setup)The current configuration is optimized for capital safety over raw volume.Risk-Reward:
By sniping at $0.82$ and stopping out at $0.65$, the bot risks roughly $0.17$ to gain $0.18$. This creates a near 1:1 risk-reward ratio,
meaning the bot only needs a >50% win rate to be profitable.Arbitrage Edge: The arbitrage logic provides "risk-free" gains that bolster the
overall P/L, acting as a cushion for the directional trades.

"""

# =========================================================
# 0. LOGGING SETTINGS
# =========================================================
EVENTS_FILE = "events/btc_15m_events_12232025.txt"
LOG_FILE = "logs/btc_15m_events_12232025_logs.txt"

def append_log(lines):
    """
    Appends a list of lines to the trade log file.
    """
    with open(LOG_FILE, "a") as f:
        for line in lines:
            f.write(line + "\n")

# =========================================================
# 1. HARDCODED TARGET SETTINGS
# =========================================================
TARGET_USDC_SIZE = 5.00
TARGET_COST = 0.43

TRADE_WINDOW_MINUTE = 15
ABORT_TRADE_WINDOW_MINUTE = 6
HARD_EXIT_SECONDS = 150

# minimum stop loss price
HARD_STOP_LOSS_PRICE = 0.35
# preserve 75% of the capital in the event of a stop loss 
CAPITAL_PRESERVATION_RATIO = 0.75
UPDATE_EFFECTIVE_STOP_LOSS_MULTIPLES = 1.20
MAX_SLIPPAGE = 0.02
# the lower the percent, the more aggressive the stop_loss_price moves up
TRAILING_STOP_PERCENT = 0.10  # Trail the peak by 10%
# TRAILING_ACTIVE_GATE_MULTIPLE decides how much the trailing stop loss gate should turn active
# for eg. ACTIVE_BID_PRICE = 0.50
# TRAILING_GATE_PRICE = ACTIVE_BID_PRICE * TRAILING_ACTIVE_GATE_MULTIPLE
# 0.575 = 0.5 * 1.15
# thus, at price 0.575, the trailing stop loss gate activates and updates the stop loss numbers
TRAILING_ACTIVE_GATE_MULTIPLE = 1.15
ASK_VS_BID_SPREAD = 0.04

# Arbitrage total cost must NOT be higher than this value
ARBITRAGE_THRESHOLD = 0.94

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
def run_events():
    event_urls = utils.load_event_urls(EVENTS_FILE)
    if not event_urls:
        print(f"❌ No events found in {EVENTS_FILE}")
        return

    for event_url in event_urls:
        print("\n" + "=" * 60)
        print(f"🎯 Processing event: {event_url}")
        try:
            run_bot(event_url)
            append_log([
                f"🟢 Run successful - {event_url}"
            ])
        except Exception as e:
            append_log([
                f"❌ Exception occurred running {event_url}: {e}"
            ])
        print("=" * 60)
    print(f"All events in {EVENTS_FILE} ran successfully")
    return

def run_bot(event_url):
    try:
        # Get token details, name of current market and end time
        # sample token: {'Up': '30153181842485352904197017700459535515576600216562347098287653572010919110026', 'Down': '34727604537658227042683904687799441550554770516513901542888684177893436283551'}
        # sample market_name: Bitcoin Up or Down - December 21, 5:00AM-5:15AM ET
        # sample end time: 2025-12-21T10:15:00Z
        tokens, market_name, end_time = utils.get_market_by_slug(utils.extract_slug(event_url))
        highest_price_seen = 0.0      # Initialize peak tracker to help with trailing stop-loss
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
        IN_POSITION = False
        SHOULD_BID = True
        BID_DIRECTION = None
        shares = 0.00
        active_token_id = None
        ACTIVE_BID_PRICE = None
        IN_ARBITRAGE = False
        EFFECTIVE_STOP_LOSS = HARD_STOP_LOSS_PRICE
        TRAILING_ACTIVE = False
        # track the last 5 prices for the ACTIVE position to filter noise
        last_ten_up_bids = deque(maxlen = 10)
        last_ten_down_bids = deque(maxlen = 10)
        
        while True:
            # 1. TIME CALCULATION
            time_left = market_end_dt - datetime.now(timezone.utc)
            seconds_left = time_left.total_seconds()
            current_minute = 15 - (seconds_left / 60)
            if seconds_left < 1:
                print(f"❌ Current market - {market_name} trading window is closed.")
                return

            # 2. FETCH PRICES
            bid_up = round(get_live_ask(up_id), 2)
            bid_down = round(get_live_ask(down_id), 2)
            if bid_up is None or bid_down is None:
                print(f"⚠️ Skipping tick: API returned None for prices. UP: {bid_up} | DOWN: {bid_down}")
                continue
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
            print(f"📖 Time Left: {seconds_left//60:.0f}m {seconds_left%60:.0f}s | In Trade?: {IN_POSITION} | ⬆️ UP Bid: {bid_up or 'N/A'} | ⬇️ DOWN Bid: {bid_down or 'N/A'}")

            if SHOULD_BID and seconds_left > ABORT_TRADE_WINDOW_MINUTE * 60 and seconds_left < TRADE_WINDOW_MINUTE * 60:
                print(f"🟢 Bid window: OPEN.")
            else:
                print(f"❌ Bid window: Closed.")

            # 2.1 HARD TIME-BASED EXIT (Liquidity Guard)
            # only applies to directional trades. Arbitrage(BOTH) runs to expiry
            if IN_POSITION and not IN_ARBITRAGE and seconds_left <= HARD_EXIT_SECONDS:
                append_log([
                    f"⏰ HARD EXIT TRIGGERED. {seconds_left:.0f}s left. Closing to avoid liquidity trap."
                ])
                print(f"⏰ HARD EXIT TRIGGERED. {seconds_left:.0f}s left. Closing to avoid liquidity trap.")
                exit_id = up_id if BID_DIRECTION == "UP" else down_id
                try:
                    hard_exit_response = execute_stop_loss(exit_id, shares, "SELL", MAX_SLIPPAGE)
                    if hard_exit_response and hard_exit_response.get("success"):
                        IN_POSITION = False
                        SHOULD_BID = False
                        append_log([f"🟢 HARD TIME EXIT SUCCESSFUL @ {seconds_left:.0f}s left."])
                        print(f"🟢 HARD TIME EXIT SUCCESSFUL @ {seconds_left:.0f}s left.")
                        # exit this event and move on to next event
                        return
                except Exception as e:
                    append_log([
                        f"❌ Hard Exit Error: {e}"
                    ])
                    print(f"❌ Hard Exit Error: {e}")

            # 2.2 CHECK FOR ARBITRAGE OPPORTUNITY
            if SHOULD_BID and not IN_POSITION:
                combined_cost = bid_up + bid_down
                # if both sides together are cheap buy both immediately
                if combined_cost <= ARBITRAGE_THRESHOLD:
                    print(f"💰 Arbitrage FOUND: BID_UP: ${bid_up}, BID_DOWN: ${bid_down}, Total cost ${combined_cost:.2f}. Attempting to buy...")
                    # buy the higher amount first, because it's a trending side so we want to catch it before it moves higher.
                    if (bid_up > bid_down):
                        # Buy UP
                        response_up, arb_up_shares = execute_with_slippage_guard(up_id, bid_up, "BUY", MAX_SLIPPAGE)
                        shares = round(arb_up_shares, 2)
                        # Leg 2: Only buy DOWN if Leg 1 worked
                        if response_up and response_up.get("success"):
                            IN_POSITION = True
                            response_down, arb_down_shares = execute_with_slippage_guard(down_id, bid_down, "BUY", MAX_SLIPPAGE)
                            if response_down and response_down.get("success"):
                                IN_ARBITRAGE = True
                                BID_DIRECTION = "BOTH"
                                append_log([f"- Arbitrage ENTRY @ total cost {combined_cost}"])
                                print("Arbitrage successful. total cost {combined_cost}")
                            continue
                    else:
                        # Buy DOWN
                        response_down, arb_down_shares = execute_with_slippage_guard(down_id, bid_down, "BUY", MAX_SLIPPAGE)
                        shares = round(arb_down_shares, 2)
                        # Leg 2 Only buy UP if Leg 1 worked
                        if response_down and response_down.get("success"):
                            IN_POSITION = True
                            response_up, arb_up_shares = execute_with_slippage_guard(up_id, bid_up, "BUY", MAX_SLIPPAGE)
                            if response_up and response_up.get("success"):
                                IN_ARBITRAGE = True
                                BID_DIRECTION = "BOTH"
                                append_log([f"- Arbitrage ENTRY @ total cost {combined_cost}"])
                                print("Arbitrage successful. total cost {combined_cost}")
                            continue

            # 3. ENTER BID WINDOW
            if IN_POSITION:
                print(f"You have an ACTIVE position. bid: {BID_DIRECTION} | shares_amount: {shares} | total_invested_size: {ACTIVE_BID_PRICE * shares} | Effective stop loss price: {EFFECTIVE_STOP_LOSS}")
            if not IN_POSITION and SHOULD_BID and not IN_ARBITRAGE and seconds_left < TRADE_WINDOW_MINUTE * 60 and seconds_left >= ABORT_TRADE_WINDOW_MINUTE * 60:
                print(f"👀 Monitoring ACTIVE. Looking for BIDS above {TARGET_COST} | UP Mean: {up_mean_price:.3f} | DOWN Mean: {down_mean_price:.3f}")
                dist_up = abs(bid_up - TARGET_COST)
                dist_down = abs(bid_down - TARGET_COST)
                
                # check eligibility for both sides independently
                target_side = None
                up_eligible = bid_up >= TARGET_COST and bid_up >= up_mean_price
                down_eligible = bid_down >= TARGET_COST and bid_down >= down_mean_price

                # decision matrix to buy UP or DOWN
                if up_eligible and down_eligible:
                    # if both hit target, pick the one closer to the target (the "purer" entry)
                    target_side = "UP" if dist_up <= dist_down else "DOWN"
                elif up_eligible:
                    target_side = "UP"
                elif down_eligible:
                    target_side = "DOWN"

                # 3.1 Bid UP section
                if target_side == "UP":
                    print(f"🔥 TREND CONFIRMED: UP ({bid_up}) >= Mean ({up_mean_price:.2f}). BUYING...")
                    try:
                        resp, shares = execute_with_slippage_guard(up_id, bid_up, "BUY", MAX_SLIPPAGE)
                        if resp and resp.get("success"):
                            IN_POSITION = True
                            SHOULD_BID = False
                            BID_DIRECTION = "UP"
                            ACTIVE_BID_PRICE = round(bid_up, 2)
                            EFFECTIVE_STOP_LOSS = round(ACTIVE_BID_PRICE * CAPITAL_PRESERVATION_RATIO, 2)
                            append_log([
                                f"✅ BUY UP @ {round(bid_up, 2)} on shares: {round(shares, 2)}. total size: {shares * bid_up}"
                            ])
                            print("✅📈 You have successfully placed a UP. Good Luck!")
                            print(f"🟢 up_id: {up_id}")
                            print(f"🟢 Purchase complete! Purchase Details:")
                            print("🟢 bid direction: UP")
                            print(f"🟢 price: {round(bid_up, 2)}")
                            print(f"🟢 shares count: {shares}")
                        else:
                            print(f"❌ Buy UP not successful!")
                    except Exception as e:
                        append_log([
                            f"❌ BUY UP Failed. UP price: {bid_up} | UP mean price: {up_mean_price:.2f} | Error: {e}"
                        ])
                        print(f"❌ BUY UP Failed. UP price: {bid_up} | UP mean price: {up_mean_price:.2f} | Error: {e}")
                # 3.2 Bid DOWN section
                elif target_side == "DOWN":
                    print(f"🔥 TREND CONFIRMED: DOWN {bid_down} >= Mean {down_mean_price:.2f}. BUYING...")
                    try:
                        resp, shares = execute_with_slippage_guard(down_id, bid_down, "BUY", MAX_SLIPPAGE)
                        if resp and resp.get("success"):
                            IN_POSITION = True
                            SHOULD_BID = False
                            BID_DIRECTION = "DOWN"
                            ACTIVE_BID_PRICE = round(bid_down, 2)
                            EFFECTIVE_STOP_LOSS = round(ACTIVE_BID_PRICE * CAPITAL_PRESERVATION_RATIO, 2)
                            append_log([
                                f"✅ BUY DOWN @ {round(bid_down, 2)} on shares: {round(shares, 2)}. total size: {shares * bid_down}"
                            ])
                            print("✅📈 You have successfully placed a bid DOWN. Good Luck!")
                            print(f"🟢 down_id: {down_id}")
                            print(f"🟢 Purchase complete! Purchase Details:")
                            print("🟢 bid direction: DOWN")
                            print(f"🟢 price: {round(bid_down, 2)}")
                            print(f"🟢 shares count: {round(shares, 2)}")
                        else:
                            print(f"❌ Buy DOWN not successful!")
                    except Exception as e:
                        append_log([
                            f"❌ BUY DOWN Failed. UP price: {bid_down} | UP mean price: {down_mean_price:.2f} | Error: {e}"
                        ])
                        print(f"❌ BUY DOWN Failed. UP price: {bid_down} | UP mean price: {down_mean_price:.2f} | Error: {e}")
            # 4. STOP LOSS LOGIC
            if IN_POSITION and not IN_ARBITRAGE:
                current_price = bid_up if BID_DIRECTION == "UP" else bid_down
                stop_loss_id = up_id if BID_DIRECTION == "UP" else down_id

                if not TRAILING_ACTIVE and current_price >= ACTIVE_BID_PRICE * TRAILING_ACTIVE_GATE_MULTIPLE:
                    print(f"🚀 THRESHOLD REACHED: Trailing stop-loss is now ACTIVE.")
                    TRAILING_ACTIVE = True
                # update the highest_price_seen to calculate dynamic stop loss price
                if current_price > highest_price_seen:
                    highest_price_seen = current_price
                    print(f"New highest price seen: ${highest_price_seen:.2f}")
                # ------------------------- DYNAMIC STOP LOSS LOGIC -----------------------
                # Calculate the dynamic trailing stop floor
                # trailing_floor = highest_price_seen * (1 - TRAILING_STOP_PERCENT)
                # Define the Profit Cap (e.g., 10% above entry)
                # This prevents the stop loss from trailing higher than your target
                # stop_loss_cap = round(ACTIVE_BID_PRICE * UPDATE_EFFECTIVE_STOP_LOSS_MULTIPLES, 2)

                # if trailing floor is below ACTIVE_BID_PRICE, update the effective_stop_loss price, otherwise leave it
                # if TRAILING_ACTIVE and (round(trailing_floor, 2) > effective_stop_loss and effective_stop_loss < stop_loss_cap):
                #     effective_stop_loss = round(min(trailing_floor, stop_loss_cap), 2)
                #     print(f"👉 📈 Effective stop loss trailing improved. New stop loss: {round(effective_stop_loss, 2)}")
                # if effective_stop_loss > ACTIVE_BID_PRICE:
                #     print(f"🔒🟢 No loss trade achieved! Good Job! Stop Loss Price: {effective_stop_loss} | Active Bid Price: {ACTIVE_BID_PRICE}")
                # # conditions to STOP SELL
                # should_sell = False
                # # rule 1: BID is UP and mean up price is below effective stop loss price
                # if (current_price and isinstance(current_price, (int, float)) and current_price <= effective_stop_loss) and (BID_DIRECTION == "UP" and up_mean_price <= effective_stop_loss):
                #     print(f"- Should sell because UP mean price is below effective_stop_loss. UP mean price: {up_mean_price}")
                #     should_sell = True
                # # rule 2: BID is DOWN and mean down price is below effective stop loss price
                # if (current_price and isinstance(current_price, (int, float)) and current_price <= effective_stop_loss) and (BID_DIRECTION == "DOWN" and down_mean_price <= effective_stop_loss):
                #     print(f"- Should sell because DOWN mean price is below effective_stop_loss. DOWN mean price: {down_mean_price}")
                #     should_sell = True
                # ------------------------- DYNAMIC STOP LOSS LOGIC -----------------------
                # rule 3: if current_price is below emergency stop loss price, sell immediately
                
                if (BID_DIRECTION == "UP" and current_price <= EFFECTIVE_STOP_LOSS):
                    print(f"- Should sell because current_price: {current_price} is below effective stop loss price: {EFFECTIVE_STOP_LOSS}")
                    should_sell = True
                elif (BID_DIRECTION == "DOWN" and current_price <= EFFECTIVE_STOP_LOSS):
                    print(f"- Should sell because current_price: {current_price} is below effective stop loss price: {EFFECTIVE_STOP_LOSS}")
                    should_sell = True
                
                if should_sell:
                    print(f"\n🚨 STOP LOSS TRIGGERED. Price dropped to ${current_price}. effective stop loss price: {EFFECTIVE_STOP_LOSS}. Attempting to SELL ALL.")
                    try:
                        sell_response = execute_stop_loss(stop_loss_id, shares, "SELL", MAX_SLIPPAGE)
                        if sell_response and sell_response.get("success"):
                            IN_POSITION = False
                            SHOULD_BID = False
                            append_log([
                                f"- STOP LOSS {BID_DIRECTION} @ {round(current_price, 2)} on shares: {round(shares, 2)}. total size: {shares * current_price}"
                            ])
                            # reset peak for next trade
                            highest_price_seen = 0.0
                            append_log([
                                f"🟢 STOP LOSS Liquidation SUCCESSFUL! ",
                                f"🟢 Liquidation Details: bid direction: {BID_DIRECTION} | token_id: {stop_loss_id} | price: {round(current_price, 2)} | shares count: {round(shares, 2)}"
                            ])
                            print(f"🟢 STOP LOSS Liquidation SUCCESSFUL! ",)
                            print(f"🟢 Liquidation Details: bid direction: {BID_DIRECTION} | token_id: {stop_loss_id} | price: {round(current_price, 2)} | shares count: {round(shares, 2)}")
                            # # successfully executed a STOP LOSS. continue to next tick
                            continue
                        else:
                            print("⚠️ Order failed to fill immediately. Retrying once...")
                            retry_response = execute_stop_loss(stop_loss_id, shares, "SELL", MAX_SLIPPAGE)
                            if retry_response and retry_response.get("success"):
                                IN_POSITION = False
                                SHOULD_BID = False
                                append_log([
                                    f"- STOP LOSS {BID_DIRECTION} @ {round(current_price, 2)} on shares: {round(shares, 2)}. total size: {shares * current_price}"
                                ])
                                # reset peak for next trade
                                highest_price_seen = 0.0
                                append_log([
                                    f"🟢 RETRY STOP LOSS Liquidation SUCCESSFUL! ",
                                    f"🟢 RETRY Liquidation Details: bid direction: {BID_DIRECTION} | token_id: {stop_loss_id} | price: {round(current_price, 2)} | shares count: {round(shares, 2)}"
                                ])
                                print(f"🟢 RETRY STOP LOSS Liquidation SUCCESSFUL! ",)
                                print(f"🟢 RETRY Liquidation Details: bid direction: {BID_DIRECTION} | token_id: {stop_loss_id} | price: {round(current_price, 2)} | shares count: {round(shares, 2)}")
                                # # successfully executed a STOP LOSS. continue to next tick
                                continue
                            else:
                                try:
                                    client.cancel_all_orders()
                                except Exception as e:
                                    append_log([
                                        f"❌ STOP LOSS CANCEL ALL ORDERS FAILED!"
                                    ])
                                    print(f"❌ STOP LOSS CANCEL ALL ORDERS FAILED!")
                                append_log([
                                    f"❌ RETRY STOP LOSS Liquidation ALSO Failed!. PLEASE MANUALLY EXECUTE STOP LOSS"
                                ])
                                print(f"❌ RETRY STOP LOSS Liquidation ALSO Failed!. PLEASE MANUALLY EXECUTE STOP LOSS")
                    except Exception as e:
                        append_log([
                            f"- ❌ STOP LOSS LIQUIDATION FAILED! ERROR: {e}",
                            f"- ❌ STOP LOSS {BID_DIRECTION} @ {round(current_price, 2)} on shares: {round(shares, 2)}. total size: {shares * current_price}"
                        ])
                        print(f"- ❌ STOP LOSS LIQUIDATION FAILED! ERROR: {e}")
                        print(f"- ❌ STOP LOSS {BID_DIRECTION} @ {round(current_price, 2)} on shares: {round(shares, 2)}. total size: {shares * current_price}")
            time.sleep(0.3)
    except Exception as e:
        append_log([
            f"Error occurred running {market_name}. Error: {e}"
        ])
        print(f"Error occurred running {market_name}. Error: {e}")
    return

def execute_with_slippage_guard(token_id, target_price, side="BUY", max_slippage = 0.02):
    """
    Checks the current book one last time before posting.
    If the price has moved more than max_slippage, it aborts.
    """
    # 1. Quick refresh: Get the very latest price
    current_ask = get_live_ask(token_id)
    current_bid = get_live_bid(token_id)

    # 2. Spread Check
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
    
    # 4. Create and Post Order
    shares = round(float(TARGET_USDC_SIZE) / current_ask, 2)
    
    print(f"-------------------Attempting to BUY-----------------------")
    print(f"BUY price: {round(current_ask, 2)}")
    print(f"BUY amount: {TARGET_USDC_SIZE}")
    print(f"-------------------Attempting to BUY-----------------------")
    order = client.create_market_order(MarketOrderArgs(
        price=round(current_ask, 2),
        amount=TARGET_USDC_SIZE,
        side=side,
        token_id=token_id,
    ))
    return client.post_order(order, OrderType.FOK), shares

def execute_stop_loss(token_id, shares, side="SELL", max_slippage = 0.02):
    # 1. Quick refresh: Get the very latest price
    current_bid = get_live_bid(token_id)
    if not current_bid:
        print("❌ Could not fetch bid price. Skipping sell.")
        return None

    # 2. Fetch live on-chain balance
    balance_info = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id
        )
    )
    balance = float(balance_info.get("balance", 0)) / 1e6
    if (balance <= 0.01):
        print(f"❌ Stop-loss aborted: No shares found in wallet (Balance: {balance})")
        return None
    # Market SELL: amount = number of shares. 
    # API Rule: Sell orders must have max 2 decimal places for the share amount.
    sell_qty = math.floor(balance * 100) / 100.0
    print(f"-------------------Attempting STOP LOSS SELL -----------------------")
    print(f"Wallet Balance: {balance}")
    print(f"Targeting Bid: {current_bid} | Selling Qty: {sell_qty}")
    print(f"-------------------Attempting STOP LOSS SELL -----------------------")

    # 3. execute the stop loss
    order = client.create_market_order(MarketOrderArgs(
        amount = round(sell_qty, 2), # number of shares to sell
        side = side,
        token_id = token_id
    ))
    return client.post_order(order, OrderType.FOK)

# def execute_stop_loss_FAK_retry(token_id, shares, side="SELL", max_slippage = 0.02):
#     """
#     Repeatedly submit FAK sell orders until all shares are sold.
#     This guarantees liquidation unless the book is completely empty.
#     """

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

def get_event_id():
    # parse event_url from CLI and extract the eventId
    parser = argparse.ArgumentParser()
    parser.add_argument("slug", help="event_url")
    args = parser.parse_args()
    return args.slug

def clamp(value, field):
    """
    Clamp price or size for Polymarket FOK orders.

    - price → max 2 decimals
    - size  → max 4 decimals
    """

    if field not in ("price", "size"):
        raise ValueError("field must be 'price' or 'size'")

    if field == "price":
        decimals = 2
    elif field == "size":
        decimals = 4
    q = Decimal("1." + "0" * decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))


if __name__ == "__main__":
    print("=== POLYMARKET Bitcoin Up or Down 15M BOT INITIALIZED ===")
    run_events()
