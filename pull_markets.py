import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs


def extract_btc_15m_events(page_url):
    """
    Crawl a Polymarket page and extract:
    - event hrefs like /event/btc-updown-15m-XXXXXXXX
    - corresponding time labels like 7:45PM
    """

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    resp = requests.get(page_url, headers=headers, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    results = []

    for a in soup.find_all("a", href=True):
        raw_href = a["href"]

        if not raw_href.startswith("/event/btc-updown-15m-"):
            continue

        # Parse href + query params
        parsed = urlparse(raw_href)
        event_path = parsed.path
        qs = parse_qs(parsed.query)

        tid = qs.get("tid", [None])[0]

        # Extract time label
        p = a.find("p")
        if not p:
            continue

        time_text = p.get_text(strip=True)

        # Rebuild full URL (preserving tid if exists)
        full_url = urljoin("https://polymarket.com", event_path)
        if tid:
            full_url += f"?tid={tid}"

        results.append({
            "time": time_text,
            "event_path": event_path,
            "tid": tid,
            "full_url": full_url
        })

    return results


if __name__ == "__main__":
    url = "https://polymarket.com/event/btc-updown-15m-1766365200?tid=1766365053083"  # example page
    events = extract_btc_15m_events(url)

    for e in events:
        print(f"🕒 {e['time']}  →  {e['href']}")