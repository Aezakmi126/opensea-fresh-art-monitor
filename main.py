import os
import time
import html
import requests
from datetime import datetime, timezone, timedelta

OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))

MAX_CREATOR_AGE_DAYS = 90
MAX_MINTS = 20

BASE_URL = "https://api.opensea.io/api/v2"

HEADERS = {
    "X-API-KEY": OPENSEA_API_KEY,
    "Accept": "application/json",
}

seen = set()


def send_telegram(text):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

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

    print("TELEGRAM STATUS:", response.status_code)

    if response.status_code != 200:
        print("TELEGRAM ERROR:", response.text)

    response.raise_for_status()


def api_get(endpoint, params=None):
    try:
        response = requests.get(
            f"{BASE_URL}{endpoint}",
            headers=HEADERS,
            params=params,
            timeout=20,
        )

        print("OPENSEA:", endpoint, response.status_code)

        if response.status_code != 200:
            print("OPENSEA ERROR:", response.text[:500])
            return {}

        return response.json()

    except requests.RequestException as error:
        print("REQUEST ERROR:", repr(error))
        return {}


def get_recent_events():
    now = int(datetime.now(timezone.utc).timestamp())

    params = [
        ("after", now - 180),
        ("event_type", "mint"),
        ("event_type", "listing"),
        ("limit", 100),
    ]

    data = api_get("/events", params=params)

    return data.get("asset_events", [])


def get_account_mints(address):
    if not address:
        return []

    params = [
        ("event_type", "mint"),
        ("limit", 21),
    ]

    data = api_get(
        f"/events/accounts/{address}",
        params=params,
    )

    return data.get("asset_events", [])


def get_collection(slug):
    if not slug:
        return {}

    return api_get(f"/collections/{slug}")


def get_account_profile(address):
    if not address:
        return {}

    return api_get(f"/accounts/{address}")


def extract_creator(event):
    if event.get("transfer_type") == "mint":
        to_address = event.get("to_address")
        if to_address:
            return to_address

    from_address = event.get("from_address")
    if from_address:
        return from_address

    maker = event.get("maker")

    if isinstance(maker, str):
        return maker

    if isinstance(maker, dict):
        address = (
            maker.get("address")
            or maker.get("wallet")
            or maker.get("account")
        )
        if address:
            return address

    from_account = event.get("from")

    if isinstance(from_account, str):
        return from_account

    if isinstance(from_account, dict):
        address = from_account.get("address")
        if address:
            return address

    return None

    


def parse_event_timestamp(event):
    value = event.get("event_timestamp")

    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(
                value,
                tz=timezone.utc,
            )
        except (ValueError, OSError):
            return None

    if isinstance(value, str):
        try:
            if value.isdigit():
                return datetime.fromtimestamp(
                    int(value),
                    tz=timezone.utc,
                )
        except (ValueError, OSError):
            pass

        try:
            value = value.replace("Z", "+00:00")

            parsed = datetime.fromisoformat(value)

            if parsed.tzinfo is None:
                parsed = parsed.replace(
                    tzinfo=timezone.utc
                )

            return parsed.astimezone(timezone.utc)

        except ValueError:
            return None

    return None


def extract_instagram(profile):
    if not profile:
        return None

    for field in [
        "instagram_username",
        "instagram",
    ]:
        value = profile.get(field)

        if isinstance(value, str) and value.strip():
            return value.replace("@", "").strip()

    socials = profile.get("socials")

    if isinstance(socials, dict):
        value = socials.get("instagram")

        if isinstance(value, str) and value.strip():
            return value.replace("@", "").strip()

    social_media = profile.get("social_media")

    if isinstance(social_media, dict):
        value = social_media.get("instagram")

        if isinstance(value, str) and value.strip():
            return value.replace("@", "").strip()

    return None


def extract_username(profile):
    if not profile:
        return None

    username = profile.get("username")

    if isinstance(username, str) and username.strip():
        return username.strip()

    return None


def score_creator(mint_count, creator_age_days, collection):
    score = 50

    if 1 <= mint_count <= 5:
        score += 25
    elif mint_count <= 10:
        score += 20
    elif mint_count <= 20:
        score += 15

    if creator_age_days <= 7:
        score += 20
    elif creator_age_days <= 30:
        score += 15
    elif creator_age_days <= 60:
        score += 10
    elif creator_age_days <= 90:
        score += 5

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
    print("DEBUG EVENT:", event)
    creator = extract_creator(event)
    print (" DEBUG CREATOR :", creator)
    if not creator:
        return

    mint_events = get_account_mints(creator)

    mint_count = len(mint_events)

    if mint_count < 1 or mint_count > MAX_MINTS:
        print("SKIP: mint_count =", mint_count)
        return

    mint_dates = []

    for mint_event in mint_events:
        mint_date = parse_event_timestamp(mint_event)

        if mint_date:
            mint_dates.append(mint_date)

    # ÐÑÐ»Ð¸ Ð´Ð°ÑÑ Ð¿ÐµÑÐ²Ð¾Ð¹ ÑÐ°Ð±Ð¾ÑÑ Ð¿ÑÐ¾Ð²ÐµÑÐ¸ÑÑ Ð½ÐµÐ»ÑÐ·Ñ,
    # Ð°Ð²ÑÐ¾ÑÐ° Ð½Ðµ Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÑÐµÐ¼
    if not mint_dates:
        return

    first_mint = min(mint_dates)

    now = datetime.now(timezone.utc)

    cutoff = now - timedelta(
        days=MAX_CREATOR_AGE_DAYS
    )

    # ÐÐµÑÐ²Ð°Ñ Ð½Ð°Ð¹Ð´ÐµÐ½Ð½Ð°Ñ ÑÐ°Ð±Ð¾ÑÐ° Ð´Ð¾Ð»Ð¶Ð½Ð° Ð±ÑÑÑ
    # Ð½Ðµ ÑÑÐ°ÑÑÐµ 90 Ð´Ð½ÐµÐ¹
    if first_mint < cutoff:
        return

    creator_age_days = max(
        0,
        (now - first_mint).days,
    )

    collection = get_collection(collection_slug)

    score = score_creator(
        mint_count,
        creator_age_days,
        collection,
    )

    if score < MIN_SCORE:
        return

    profile = get_account_profile(creator)

    username = extract_username(profile)
    instagram = extract_instagram(profile)

    collection_name = (
        collection.get("name")
        or collection_slug
        or "Untitled"
    )

    # ÐÐ°ÑÐ¸ÑÐ° Telegram HTML Ð¾Ñ ÑÐ¿ÐµÑÐ¸Ð°Ð»ÑÐ½ÑÑ ÑÐ¸Ð¼Ð²Ð¾Ð»Ð¾Ð²
    safe_name = html.escape(str(name))
    safe_collection = html.escape(str(collection_name))
    safe_creator = html.escape(str(creator))

    nft_url = (
        f"https://opensea.io/assets/"
        f"{chain}/{contract}/{token_id}"
    )

    creator_url = f"https://opensea.io/{creator}"

    first_mint_text = first_mint.strftime(
        "%Y-%m-%d"
    )

    if instagram:
        instagram = instagram.strip()

        instagram_url = (
            f"https://instagram.com/{instagram}"
        )

        safe_instagram = html.escape(instagram)

        instagram_line = (
            f'📷 Instagram: '
            f'<a href="{instagram_url}">'
            f'@{safe_instagram}</a>'
        )
    else:
        instagram_line = "📷 Instagram: не найден"

    if username:
        username_line = (
            "🌊 OpenSea: "
            + html.escape(str(username))
        )
    else:
        username_line = "🌊 OpenSea: имя не найдено"

    if score >= 90:
        badge = "🔥"
    elif score >= 80:
        badge = "⭐"
    else:
        badge = "🆕"

    message = f"""
{badge} <b>Новый NFT-автор — рейтинг {score}/100</b>

🎨 <b>{safe_name}</b>
📁 {safe_collection}

{username_line}

👛 <b>Кошелёк:</b>
<code>{safe_creator}</code>

{instagram_line}

🖼 <b>Количество mint-событий:</b> {mint_count}
📅 <b>Первый mint:</b> {first_mint_text}
⏳ <b>Возраст автора:</b> {creator_age_days} дней

🔄 <b>Событие:</b> {html.escape(str(event.get("event_type")))}

🔗 <a href="{nft_url}">Посмотреть работу на OpenSea</a>
👤 <a href="{creator_url}">Открыть автора на OpenSea</a>

<i>Фильтр: 1–20 mint-событий, возраст автора не старше 90 дней.</i>
"""

    send_telegram(message


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
            "Missing variables: "
            + ", ".join(missing)
        )


def main():
    check_configuration()

    print("OpenSea Fresh Art Monitor started")

    send_telegram( "<b>OpenSea Fresh Art Monitor запущен</b>\n"
        "Ищу новых авторов:\n"
        "• 1–20 NFT\n"
        "• возраст автора до 90 дней\n"
        "• новые работы и минты\n"
        "• ссылки на OpenSea и Instagram"
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
