import os
import time
import datetime
import requests # Added for Gamma API
from dotenv import load_dotenv
import ccxt
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs

# 1. HARDCODED EVENT SETTINGS
EVENT_SERIES_NAME = "Bitcoin Up or Down" # The "Event" name filter
TARGET_PAIR_COST = 0.85                 # Total cost for 1 Up + 1 Down
TARGET_USDC_PER_LEG = 1                       # Number of shares to buy
ANCHOR_PRICE = 0.35                     # Price at which to buy Leg 1
KILL_SWITCH_MINUTE = 11                 # Minute to exit if not hedged

# 2. BOT SETUP
load_dotenv()
PK = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")

CREDS = ApiCreds(
    api_key=os.getenv("CLOB_API_KEY"),
    api_secret=os.getenv("CLOB_SECRET"),
    api_passphrase=os.getenv("CLOB_PASSPHRASE")
)

# Initialize Client (signature_type=2 for Phantom/MetaMask)
client = ClobClient(
    host="https://clob.polymarket.com", 
    key=PK, 
    chain_id=137, 
    funder=FUNDER, 
    signature_type=2
)
client.set_api_creds(CREDS)
exchange = ccxt.binance()

def find_latest_window():
    """Uses Gamma API to robustly find the current active 15m window."""
    print(f"Scanning Gamma API for the latest {EVENT_SERIES_NAME} window...")
    try:
        # Gamma API is the source of truth for market discovery
        url = "https://gamma-api.polymarket.com/markets"
        params = {
            "active": "true",
            "closed": "false",
            "order": "endDate",
            "ascending": "true",
            "limit": 50
        }
        
        resp = requests.get(url, params=params)
        markets_list = resp.json()
        
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        for m in markets_list:
            question = m.get('question', '')
            
            # Filter for your specific series
            if EVENT_SERIES_NAME in question:
                expiry_str = m.get('endDate')
                if not expiry_str: continue
                
                # Parse ISO date
                expiry = datetime.datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
                
                if expiry > now_utc:
                    # Extract Token IDs from clobTokenIds field
                    token_ids = m.get('clobTokenIds')
                    # outcomes is usually a list like ["Down", "Up"]
                    outcomes = m.get('outcomes')
                    
                    # Handle cases where outcomes might be a string string
                    if isinstance(outcomes, str):
                        import ast
                        outcomes = ast.literal_eval(outcomes)

                    if token_ids and outcomes:
                        tokens = {outcomes[i]: token_ids[i] for i in range(len(outcomes))}
                        return tokens, question

        return None, None

    except Exception as e:
        print(f"Gamma API Error: {e}")
        return None, None

def start_arb_loop():
    tokens, market_name = find_latest_window()
    if not tokens or 'Up' not in tokens or 'Down' not in tokens:
        print("No active window found. Re-scanning in 15s...")
        return

    print(f"\n✅ TRADING WINDOW: {market_name}")
    up_id = tokens['Up']
    down_id = tokens['Down']
    
    leg1_filled = False
    leg2_filled = False
    leg2_order_id = None
    start_time = time.time()

    while True:
        elapsed_mins = (time.time() - start_time) / 60
        
        # --- PHASE 1: KILL SWITCH (MINUTE 12) ---
        if elapsed_mins >= KILL_SWITCH_MINUTE:
            if leg1_filled and not leg2_filled:
                print("\n[!] TIMEOUT: Exiting orphaned trade to prevent loss.")
                if leg2_order_id: 
                    try:
                        client.cancel_order(leg2_order_id)
                    except:
                        pass
                return 
            if leg1_filled and leg2_filled:
                print("\n[+] SUCCESS: Arbitrage complete for this window.")
                return
            if not leg1_filled:
                print("\n[.] Window ending. No entry found.")
                return

        # --- PHASE 2: MONITOR & EXECUTE ---
        try:
            ticker = exchange.fetch_ticker('BTC/USDT')
            btc = ticker['last']
            print(f"Min: {round(elapsed_mins, 1)} | BTC: ${btc} | Leg1: {leg1_filled} | Leg2: {leg2_filled}", end='\r')

            if not leg1_filled:
                ob = client.get_order_book(down_id)
                asks = ob.asks if hasattr(ob, 'asks') else ob.get('asks', [])
                
                if asks:
                    best_ask = float(asks[0].price if hasattr(asks[0], 'price') else asks[0]['price'])
                    
                    if best_ask <= ANCHOR_PRICE:
                        print(f"\n🚀 TRIGGER: Buying 'Down' at ${best_ask}")
                        order = client.create_order(OrderArgs(price=best_ask, size=TARGET_USDC_PER_LEG, side="BUY", token_id=down_id))
                        resp = client.post_order(order)
                        
                        if resp.get("success"):
                            leg1_filled = True
                            target_hedge_price = round(TARGET_PAIR_COST - best_ask, 2)
                            print(f"⚓ Leg 1 Filled. Placing Leg 2 (Up) Limit Order at ${target_hedge_price}")
                            
                            h_order = client.create_order(OrderArgs(price=target_hedge_price, size=TARGET_USDC_PER_LEG, side="BUY", token_id=up_id))
                            h_resp = client.post_order(h_order)
                            leg2_order_id = h_resp.get("orderID")

            if leg1_filled and not leg2_filled and leg2_order_id:
                status = client.get_order(leg2_order_id)
                if status and status.get("status") == "FILLED":
                    print("\n💰 PROFIT LOCKED: Both sides of the Arb are filled.")
                    leg2_filled = True

        except Exception as e:
            print(f"\nExecution Error: {e}")
        
        time.sleep(1.5)

if __name__ == "__main__":
    print("=== BTC 15M ARB BOT INITIALIZED (GAMMA API MODE) ===")
    while True:
        start_arb_loop()
        time.sleep(15)