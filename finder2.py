import requests

# Fetch events and filter by slug prefix
def get_events_by_slug_prefix(prefix, closed=False, limit=100):
    response = requests.get(
        "https://gamma-api.polymarket.com/public-search",
        params={"q": "btc-updown-15m"}
    )
    results = response.json()
    print(results)

if __name__ == "__main__":
    get_events_by_slug_prefix("btc-updown-15m-")