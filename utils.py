import ast
import os
import re
import requests
from typing import List, Tuple

# =========================================================
# 4. UTILITY FUNCTIONS
# =========================================================
def get_market_by_slug(slug):
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
        print(f"🟢 Successfully fetched market data")
        print(f"✅ tokens: {tokens}")
        print(f"✅ market_name: {data.get("question")}")
        print(f"✅ market_end_time: {end_time_str}")
        print()
        return tokens, data.get("question"), end_time_str

    except Exception as e:
        print(f"Slug lookup error: {e}")
        return None, None, None

def extract_slug(url: str) -> str:
    """
    Extracts the slug like 'btc-updown-15m-1766290500' from a Polymarket event URL.
    """
    match = re.search(r"event/([a-zA-Z0-9\-]+)\?", url)
    if match:
        return match.group(1)
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

def load_event_urls(file_path="events.txt"):
    """
    Loads event URLs from a text file (one per line).
    Ignores empty lines and comments.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Event file not found: {file_path}")

    with open(file_path, "r") as f:
        events = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]

    return events

def read_polymarket_events(file_path: str) -> List[Tuple[str, str]]:
    """
    Reads a file containing Polymarket event lines and extracts
    the event URL and event name.

    Expected line format:
    <URL> - <EVENT_NAME>

    Returns:
        List of (url, event_name)
    """
    events = []

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Split only on the first delimiter
            parts = line.split(" - ", 1)
            if len(parts) != 2:
                continue  # skip malformed lines

            url, event_name = parts
            events.append((url.strip(), event_name.strip()))

    return events


if __name__ == "__main__":
    events = read_polymarket_events("events/btc_15m_events_12242025.txt")
    for url, name in events:
        print(f"{url} - {name}")
