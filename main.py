import os
import time
import requests
from datetime import datetime, timezone

OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))

BASE_URL = "https://api.opensea.io/api/v2"

HEADERS = {
    "X-API-KEY": OPENSEA_API_KEY,
    "Accept": "application/json",
}

seen = set()


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )

    response.raise_for_status()


def get_recent_events():
    now = int(datetime.now(timezone.utc).timestamp())

    params = [
        ("after", now - 180),
        ("event_type", "mint"),
        ("event_type", "listing"),
        ("limit", 100),
    ]

    response = requests.get(
        f"{BASE_URL}/events",
        headers=HEADERS,
        params=params,
        timeout=20,
    )

    response.raise_for_status()

    return response.json().get("asset_events", [])


def get_account_events(address):
    if not address:
        return []

    params = [
        ("event_type", "mint"),
        ("limit", 200),
    ]

    response = requests.get(
        f"{BASE_URL}/events/accounts/{address}",
        headers=HEADERS,
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        return []

    return response.json().get("asset_events", [])


def get_collection(slug):
    if not slug:
        return {}

    response = requests.get(
        f"{BASE_URL}/collections/{slug}",
        headers=HEADERS,
        timeout=20,
    )

    if response.status_code != 200:
        return {}

    return response.json()


def extract_creator(event):
    maker = event.get("maker")

    if isinstance(maker, str):
        return maker

    if isinstance(maker, dict):
        return (
            maker.get("address")
            or maker.get("wallet")
            or maker.get("account")
        )

    from_account = event.get("from")

    if isinstance(from_account, str):
        return from_account

    if isinstance(from_account, dict):
        return from_account.get("address")

    return None


def score_creator(mint_count, collection):
    score = 50

    if 1 <= mint_count <= 5:
        score += 30
    elif mint_count <= 10:
        score += 25
    elif mint_count <= 20:
        score += 20
    else:
        score -= 40

    stats = collection.get("stats", {}) or {}

    owners = (
        stats.get("num_owners")
        or stats.get("owner_count")
        or 0
    )

    volume = (
        stats.get("total_volume")
        or stats.get("volume")
        or 0
    )

    if isinstance(owners, (int, float)):
        if owners <= 10:
            score += 10
        elif owners > 500:
            score -= 15

    if isinstance(volume, (int, float)):
        if volume == 0:
            score += 10
        elif volume > 50:
            score -= 15

    return max(0, min(score, 100))


def process_event(event):
    nft = event.get("nft") or {}

    token_id = nft.get("identifier")
    contract = nft.get("contract")
    collection_slug = nft.get("collection")
    name = nft.get("name") or f"NFT #{token_id}"
    chain = nft.get("chain") or "ethereum"

    if not contract or token_id is None:
        return

    unique_id = f"{chain}:{contract}:{token_id}"

    if unique_id in seen:
        return

    seen.add(unique_id)

    creator = extract_creator(event)

    if not creator:
        return

    creator_events = get_account_events(creator)

    mint_count = len(
        [
            event
            for event in creator_events
            if event.get("event_type") == "mint"
        ]
    )

    if mint_count < 1 or mint_count > 20:
        return

    collection = get_collection(collection_slug)

    score = score_creator(
        mint_count,
        collection,
    )

    if score < MIN_SCORE:
        return

    collection_name = (
        collection.get("name")
        or collection_slug
        or "Untitled"
    )

    nft_url = (
        f"https://opensea.io/assets/"
        f"{chain}/{contract}/{token_id}"
    )

    creator_url = f"https://opensea.io/{creator}"

    if score >= 90:
        badge = "HOT"
    elif score >= 80:
        badge = "STRONG"
    else:
        badge = "NEW"

    message = f"""
<b>{badge} - NEW CREATOR - {score}/100</b>

<b>{name}</b>
Collection: {collection_name}

Creator:
<code>{creator}</code>

Mint events found: <b>{mint_count}</b>
Event: <b>{event.get("event_type")}</b>

<a href="{nft_url}">Open NFT on OpenSea</a>
<a href="{creator_url}">Open creator profile</a>

<i>Score estimates how small/new the creator appears.
It is not a price forecast.</i>
"""

    send_telegram(message)


def check_configuration():
    missing = []

    if not OPENSEA_API_KEY:
        missing.append("OPENSEA_API_KEY")

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Missing variables: " + ", ".join(missing)
        )


def main():
    check_configuration()

    print("OpenSea Fresh Art Monitor started")

    send_telegram(
        "<b>OpenSea Fresh Art Monitor started</b>\n\n"
        "Searching for fresh works from creators "
        "with roughly 1-20 mint events."
    )

    while True:
        try:
            events = get_recent_events()

            print(
                datetime.now().strftime("%H:%M:%S"),
                "events:",
                len(events),
            )

            for event in events:
                process_event(event)

        except Exception as error:
            print("ERROR:", repr(error))

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
