import re

# Read the uploaded HTML content
with open('event_html.txt', 'r', encoding='utf-8') as f:
    content = f.read()

# Polmarket HTML contains JSON data for related/subsequent markets.
# We need to extract the slug and title for December 25 events.
# Pattern: "slug":"...","title":"Bitcoin Up or Down - December 25..."
# We also need the "id" to construct the tid parameter.

# Find all JSON objects that look like market entries
# Example: {"id":"123","slug":"btc-updown-15m-1766640000","title":"Bitcoin Up or Down - December 25, 12:00AM-12:15AM ET", ...}
matches = re.findall(r'\{"id":"(\d+)","slug":"(btc-updown-15m-\d+)","title":"(Bitcoin Up or Down - December 26, [^"]+)"', content)

results = []
seen_slugs = set()

for tid, slug, title in matches:
    if slug not in seen_slugs:
        url = f"https://polymarket.com/event/{slug}?tid={tid}"
        results.append(f"{url} - {title}")
        seen_slugs.add(slug)

# If standard regex is too narrow, try a more flexible search
if not results:
    slugs = re.findall(r'"slug":"(btc-updown-15m-\d+)"', content)
    for slug in set(slugs):
        # Find the specific ID and Title associated with this slug in the same JSON block
        # Look for the block containing the slug and extract id/title
        block_match = re.search(r'\{[^{}]*?"id":"(\d+)"[^{}]*?"slug":"' + slug + r'"[^{}]*?"title":"(Bitcoin Up or Down - December 26, [^"]+)"', content)
        if block_match:
            tid, title = block_match.groups()
            url = f"https://polymarket.com/event/{slug}?tid={tid}"
            line = f"{url} - {title}"
            if line not in results:
                results.append(line)

# Sort by time for better readability (extracting time from title)
def sort_key(s):
    time_match = re.search(r'(\d{1,2}:\d{2})(AM|PM)', s)
    if time_match:
        time_str, ampm = time_match.groups()
        hour, minute = map(int, time_str.split(':'))
        if ampm == 'PM' and hour != 12: hour += 12
        if ampm == 'AM' and hour == 12: hour = 0
        return hour * 60 + minute
    return 0

results.sort(key=sort_key)

for res in results:
    print(res)