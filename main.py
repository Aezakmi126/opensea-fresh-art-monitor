import os
import time
import html
import requests
from datetime import datetime, timezone, timedelta
from collections import Counter
from urllib.parse import urlparse
import re

OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "55"))

# ЖЁСТКИЙ режим поиска именно свежих авторов.
# Можно менять через Variables в Railway/Render, не трогая код.
MAX_CREATOR_AGE_DAYS = int(os.getenv("MAX_CREATOR_AGE_DAYS", "30"))
MAX_MINTS = int(os.getenv("MAX_MINTS", "20"))
MAX_COLLECTION_MINTS = int(os.getenv("MAX_COLLECTION_MINTS", "30"))
RECENT_LISTING_DAYS = int(os.getenv("RECENT_LISTING_DAYS", "30"))
DISCOVERY_WINDOW_SECONDS = int(os.getenv("DISCOVERY_WINDOW_SECONDS", "1800"))
DISCOVERY_PAGES = int(os.getenv("DISCOVERY_PAGES", "3"))
DISCOVERY_EVENTS_PER_PAGE = int(os.getenv("DISCOVERY_EVENTS_PER_PAGE", "100"))
REQUIRE_RECENT_LISTING = os.getenv("REQUIRE_RECENT_LISTING", "1") == "1"
REQUIRE_INSTAGRAM = os.getenv("REQUIRE_INSTAGRAM", "1") == "1"

BASE_URL = "https://api.opensea.io/api/v2"

HEADERS = {
    "X-API-KEY": OPENSEA_API_KEY,
    "Accept": "application/json",
}

# Коллекции/слова, которые чаще всего дают не художников, а игровые,
# финансовые, массовые или служебные NFT.
BLOCKED_COLLECTIONS = {
    "courtyard-nft",
}

SERVICE_NFT_KEYWORDS = [
    "uniswap",
    "positions nft",
    "position nft",
    "liquidity position",
    "liquidity",
    "lp position",
    "staking",
    "staked",
    "aave",
    "compound",
    "curve",
    "balancer",
    "pancakeswap",
    "sushiswap",
    "ens",
    "ethereum name service",
    "name wrapper",
    "bridge",
    "receipt",
    "vault",
    "ticket",
    "membership",
    "pass",
]

# seen_candidates не даёт проверять один и тот же кошелёк каждую минуту.
# seen_nfts не даёт отправлять одну и ту же работу повторно.
seen_candidates = set()
seen_nfts = set()
stats = {}


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

    print("TELEGRAM STATUS:", response.status_code)

    if response.status_code != 200:
        print("TELEGRAM ERROR:", response.text[:500])

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


def _event_discovery_key(event):
    nft = event.get("nft") or event.get("asset") or {}
    chain = nft.get("chain") or event.get("chain") or ""
    contract = nft.get("contract") or ""
    token_id = nft.get("identifier")
    tx = event.get("transaction") or event.get("transaction_hash") or ""
    recipient = extract_mint_recipient(event) or ""
    return f"{chain}:{contract}:{token_id}:{tx}:{recipient}"


def get_recent_mint_events():
    """Берём несколько страниц СВЕЖИХ mint-событий, а не только последние 100.

    Это расширяет выборку и повышает шанс увидеть маленькую новую коллекцию,
    вместо того чтобы постоянно крутиться вокруг самых массовых mint-потоков.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    after = now - DISCOVERY_WINDOW_SECONDS

    all_events = []
    seen = set()
    cursor = None

    pages = max(1, min(DISCOVERY_PAGES, 5))
    per_page = max(20, min(DISCOVERY_EVENTS_PER_PAGE, 100))

    for page in range(pages):
        params = [
            ("after", after),
            ("event_type", "mint"),
            ("limit", per_page),
        ]
        if cursor:
            params.append(("next", cursor))

        data = api_get("/events", params=params)
        events = data.get("asset_events", []) or []

        added = 0
        for event in events:
            key = _event_discovery_key(event)
            if key in seen:
                continue
            seen.add(key)
            all_events.append(event)
            added += 1

        print(
            "DISCOVERY PAGE:",
            page + 1,
            "events =", len(events),
            "new =", added,
            "total =", len(all_events),
        )

        next_cursor = data.get("next")
        if not next_cursor or not events or added == 0:
            break
        cursor = next_cursor

    return all_events


def get_account_mints(address, chain):
    """Получаем до MAX_MINTS + 1 mint-событий кошелька.

    Если вернулось больше MAX_MINTS или OpenSea дал next-cursor,
    кошелёк уже слишком активный и новым автором не считается.

    ВАЖНО: передаём chain. В старом коде chain не передавался, поэтому
    для Base/Polygon фактически проверялся Ethereum — это могло давать
    ложные результаты.
    """
    if not address:
        return [], False

    limit = min(MAX_MINTS + 1, 200)
    params = [
        ("event_type", "mint"),
        ("chain", chain),
        ("limit", limit),
    ]

    data = api_get(f"/events/accounts/{address}", params=params)
    events = data.get("asset_events", [])
    has_more = bool(data.get("next"))

    print(
        "ACCOUNT_MINTS:", address,
        "chain =", chain,
        "count =", len(events),
        "has_more =", has_more,
    )

    return events, has_more


def get_recent_account_listings(address, chain):
    if not address:
        return []

    after = int(
        (datetime.now(timezone.utc) - timedelta(days=RECENT_LISTING_DAYS)).timestamp()
    )

    params = [
        ("after", after),
        ("event_type", "listing"),
        ("chain", chain),
        ("limit", 20),
    ]

    data = api_get(f"/events/accounts/{address}", params=params)
    return data.get("asset_events", [])


def get_collection_mints(slug):
    """Считаем, не является ли коллекция уже массовой.

    Нам не нужно знать точное число, если работ много. Достаточно получить
    MAX_COLLECTION_MINTS + 1 событий: это сразу основание для отказа.
    """
    if not slug:
        return [], False

    limit = min(MAX_COLLECTION_MINTS + 1, 200)
    params = [
        ("event_type", "mint"),
        ("limit", limit),
    ]

    data = api_get(f"/events/collection/{slug}", params=params)
    events = data.get("asset_events", [])
    has_more = bool(data.get("next"))

    return events, has_more


def get_collection(slug):
    if not slug:
        return {}
    return api_get(f"/collections/{slug}")


def get_collection_stats(slug):
    if not slug:
        return {}
    return api_get(f"/collections/{slug}/stats")


def get_account_profile(address):
    if not address:
        return {}
    return api_get(f"/accounts/{address}")



def extract_collection_creator(collection):
    """Пытаемся получить владельца/создателя коллекции из метаданных OpenSea.

    Для поиска художника это предпочтительнее, чем автоматически считать
    получателя mint автором: получатель часто оказывается коллекционером.
    """
    if not isinstance(collection, dict):
        return None

    preferred_keys = (
        "creator", "creator_address", "owner", "owner_address",
        "payout_address", "payment_address"
    )

    for key in preferred_keys:
        address = normalize_address(collection.get(key))
        if address:
            return address

    # Иногда адрес находится во вложенном объекте.
    for key in preferred_keys:
        value = collection.get(key)
        if isinstance(value, dict):
            address = normalize_address(value)
            if address:
                return address

    return None


def normalize_address(value):
    if isinstance(value, str) and value.startswith("0x"):
        return value.lower()
    if isinstance(value, dict):
        address = value.get("address") or value.get("wallet") or value.get("account")
        if isinstance(address, str) and address.startswith("0x"):
            return address.lower()
    return None


def extract_mint_recipient(event):
    """Для mint берём кошелёк, который получил свежесозданный NFT.

    Это значительно безопаснее, чем брать maker из listing-события.
    """
    for key in ("to_address", "to"):
        address = normalize_address(event.get(key))
        if address:
            return address

    # Запасные поля на случай другого формата события.
    for key in ("maker", "account"):
        address = normalize_address(event.get(key))
        if address:
            return address

    return None



def is_mint_event(event):
    """OpenSea может отдавать mint как event_type=mint или как transfer + transfer_type=mint."""
    event_type = str(event.get("event_type") or "").lower()
    transfer_type = str(event.get("transfer_type") or "").lower()
    return event_type == "mint" or (event_type == "transfer" and transfer_type == "mint")


def print_discovery_diagnostics(events):
    """Короткая диагностика каждого цикла: где теряются кандидаты."""
    event_types = Counter(str(e.get("event_type") or "missing") for e in events)
    transfer_types = Counter(str(e.get("transfer_type") or "missing") for e in events)
    mint_like = sum(1 for e in events if is_mint_event(e))
    with_nft = sum(1 for e in events if (e.get("nft") or e.get("asset")))
    with_recipient = sum(1 for e in events if extract_mint_recipient(e))

    print(
        "DIAG DISCOVERY:",
        "events =", len(events),
        "| mint_like =", mint_like,
        "| with_nft =", with_nft,
        "| with_recipient =", with_recipient,
        "| event_types =", dict(event_types),
        "| transfer_types =", dict(transfer_types),
    )

def parse_event_timestamp(event):
    value = event.get("event_timestamp")

    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError):
            return None

    if isinstance(value, str):
        try:
            if value.isdigit():
                return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, OSError):
            pass

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    return None


def extract_instagram(profile):
    if not profile:
        return None

    for field in ("instagram_username", "instagram"):
        value = profile.get(field)
        if isinstance(value, str) and value.strip():
            return value.replace("@", "").strip()

    for container_name in ("socials", "social_media"):
        container = profile.get(container_name)
        if isinstance(container, dict):
            value = container.get("instagram")
            if isinstance(value, str) and value.strip():
                return value.replace("@", "").strip()

    return None



def _instagram_from_text(value):
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    match = re.search(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})(?:[/?#]|$)", raw, flags=re.IGNORECASE)
    if match:
        username = match.group(1).strip(".")
        if username.lower() not in {"accounts","about","developer","explore","p","reel","reels","stories","direct","legal","privacy"}:
            return username
    return None

def extract_instagram_deep(data):
    direct = extract_instagram(data if isinstance(data, dict) else {})
    if direct:
        return direct
    stack=[data]; seen=set()
    while stack:
        value=stack.pop(); obj_id=id(value)
        if obj_id in seen: continue
        seen.add(obj_id)
        if isinstance(value, dict):
            for key,item in value.items():
                key_l=str(key).lower()
                if "instagram" in key_l and isinstance(item,str) and item.strip():
                    found=_instagram_from_text(item)
                    if found: return found
                    candidate=item.replace("@","").strip()
                    if re.fullmatch(r"[A-Za-z0-9._]{1,30}", candidate): return candidate
                stack.append(item)
        elif isinstance(value,(list,tuple,set)):
            stack.extend(value)
        elif isinstance(value,str):
            found=_instagram_from_text(value)
            if found: return found
    return None

def extract_external_urls(data):
    urls=[]; stack=[data]; seen=set()
    while stack:
        value=stack.pop(); obj_id=id(value)
        if obj_id in seen: continue
        seen.add(obj_id)
        if isinstance(value,dict): stack.extend(value.values())
        elif isinstance(value,(list,tuple,set)): stack.extend(value)
        elif isinstance(value,str):
            for match in re.findall(r'https?://[^\s"\'<>]+', value):
                cleaned=match.rstrip('.,);]')
                if cleaned not in urls: urls.append(cleaned)
    return urls[:12]

def find_instagram_on_external_pages(*sources):
    urls=[]
    for source in sources: urls.extend(extract_external_urls(source))
    checked=set()
    for url in urls:
        if url in checked: continue
        checked.add(url)
        try:
            parsed=urlparse(url); host=(parsed.hostname or '').lower()
            if parsed.scheme not in ('http','https'): continue
            if not host or host in {'localhost','127.0.0.1','::1'} or host.endswith('.local'): continue
            direct=_instagram_from_text(url)
            if direct:
                print('INSTAGRAM FALLBACK: found direct URL =', url); return direct
            response=requests.get(url, timeout=8, allow_redirects=True, headers={'User-Agent':'Mozilla/5.0'})
            if response.status_code >= 400: continue
            found=_instagram_from_text(response.url)
            if found:
                print('INSTAGRAM FALLBACK: found after redirect =', response.url); return found
            body=response.text[:500000]
            found=_instagram_from_text(body)
            if found:
                print('INSTAGRAM FALLBACK: found on external page =', url); return found
        except requests.RequestException as error:
            print('INSTAGRAM FALLBACK ERROR:', url, repr(error))
    return None

def extract_username(profile):
    if not profile:
        return None
    username = profile.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    return None


def contains_service_keyword(name, collection_slug, collection_name=""):
    text = f"{name or ''} {collection_slug or ''} {collection_name or ''}".lower()
    return any(keyword in text for keyword in SERVICE_NFT_KEYWORDS)


def score_creator(mint_count, creator_age_days, collection_mint_count, profile, stats):
    score = 0

    # Чем меньше работ — тем интереснее для нашей задачи.
    if 1 <= mint_count <= 3:
        score += 35
    elif mint_count <= 5:
        score += 30
    elif mint_count <= 10:
        score += 22
    elif mint_count <= 20:
        score += 12

    # Главный вес: насколько недавно вообще появился первый mint.
    if creator_age_days <= 1:
        score += 35
    elif creator_age_days <= 3:
        score += 30
    elif creator_age_days <= 7:
        score += 25
    elif creator_age_days <= 14:
        score += 18
    elif creator_age_days <= 30:
        score += 10

    # Маленькая свежая коллекция лучше массового проекта.
    if 1 <= collection_mint_count <= 5:
        score += 15
    elif collection_mint_count <= 10:
        score += 10
    elif collection_mint_count <= 30:
        score += 5

    username = extract_username(profile)
    instagram = extract_instagram(profile)

    if username:
        score += 8
    if instagram:
        score += 12

    # Небольшой бонус нулевой/малой торговой истории.
    total = stats.get("total", {}) if isinstance(stats, dict) else {}
    volume = 0
    if isinstance(total, dict):
        volume = total.get("volume", 0) or 0
    elif isinstance(stats, dict):
        volume = stats.get("total_volume", 0) or 0

    if isinstance(volume, (int, float)):
        if volume == 0:
            score += 5
        elif volume > 50:
            score -= 15

    return max(0, min(score, 100))


def process_event(event):
    if not is_mint_event(event):
        print(
            "SKIP: not a mint-like event | event_type =",
            event.get("event_type"),
            "| transfer_type =",
            event.get("transfer_type"),
        )
        return

    nft = event.get("nft") or event.get("asset") or {}

    token_id = nft.get("identifier")
    contract = nft.get("contract")
    collection_slug = nft.get("collection")
    name = nft.get("name") or f"NFT #{token_id}"
    chain = nft.get("chain") or event.get("chain") or "ethereum"

    if not contract or token_id is None:
        print("SKIP: missing NFT data")
        return

    if collection_slug in BLOCKED_COLLECTIONS:
        print("SKIP: blocked collection =", collection_slug)
        return

    if contains_service_keyword(name, collection_slug):
        print("SKIP: service/mass NFT =", name, "|", collection_slug)
        return

    unique_nft_id = f"{chain}:{contract.lower()}:{token_id}"
    if unique_nft_id in seen_nfts:
        return
    seen_nfts.add(unique_nft_id)

    # 1) Сначала проверяем КОЛЛЕКЦИЮ.
    # Массовые коллекции отбрасываем до дорогих запросов истории кошелька.
    collection = get_collection(collection_slug)
    collection_name = collection.get("name") or collection_slug or "Без названия"

    if contains_service_keyword(name, collection_slug, collection_name):
        print("SKIP: service keyword after collection lookup")
        return

    collection_mints, collection_has_more = get_collection_mints(collection_slug)
    collection_mint_count = len(collection_mints)

    if collection_has_more or collection_mint_count > MAX_COLLECTION_MINTS:
        print(
            "SKIP: collection too large |",
            collection_slug,
            "mint_count =", collection_mint_count,
            "has_more =", collection_has_more,
        )
        return

    # 2) Кандидат на автора.
    # Если OpenSea сообщает owner/creator коллекции — используем его.
    # Получатель mint остаётся fallback, когда owner/creator отсутствует.
    collection_creator = extract_collection_creator(collection)
    mint_recipient = extract_mint_recipient(event)

    if collection_creator:
        creator = collection_creator
        creator_source = "collection_owner"
    else:
        creator = mint_recipient
        creator_source = "mint_recipient"

    print(
        "CANDIDATE WALLET:",
        creator,
        "| source =", creator_source,
        "| chain =", chain,
        "| collection =", collection_slug,
        "| collection_mints =", collection_mint_count,
    )

    if not creator:
        print("SKIP: no creator/owner and no mint recipient")
        return

    candidate_key = f"{chain}:{creator}"
    if candidate_key in seen_candidates:
        print("SKIP: candidate already checked =", candidate_key)
        return

    seen_candidates.add(candidate_key)
    stats["checked"] = stats.get("checked", 0) + 1

    # 3) Проверяем историю mint выбранного автора.
    mint_events, account_has_more = get_account_mints(creator, chain)
    mint_count = len(mint_events)

    if account_has_more or mint_count < 1 or mint_count > MAX_MINTS:
        stats["too_many_mints"] = stats.get("too_many_mints", 0) + 1
        print(
            "SKIP: creator mint history outside target |",
            "source =", creator_source,
            "count =", mint_count,
            "has_more =", account_has_more,
        )
        return

    mint_dates = [
        dt for dt in (parse_event_timestamp(x) for x in mint_events)
        if dt is not None
    ]

    if not mint_dates:
        print("SKIP: no valid mint dates")
        return

    first_mint = min(mint_dates)
    now = datetime.now(timezone.utc)
    creator_age_days = max(0, (now - first_mint).days)

    if creator_age_days > MAX_CREATOR_AGE_DAYS:
        stats["too_old"] = stats.get("too_old", 0) + 1
        print(
            "SKIP: creator too old | age_days =",
            creator_age_days,
            "first_mint =",
            first_mint,
        )
        return

    # 4) Instagram: ищем в профиле, коллекции и внешних ссылках.
    profile = get_account_profile(creator)
    username = extract_username(profile)
    instagram = extract_instagram_deep(profile)

    if not instagram:
        instagram = extract_instagram_deep(collection)

    if not instagram:
        instagram = find_instagram_on_external_pages(profile, collection)

    if REQUIRE_INSTAGRAM and not instagram:
        print("SKIP: no Instagram after fallback search")
        return

    if instagram:
        print("INSTAGRAM FOUND:", instagram)

    # 5) Новый автор должен реально выставлять работы, а не только получать mint/drop.
    recent_listings = get_recent_account_listings(creator, chain)

    if REQUIRE_RECENT_LISTING and not recent_listings:
        print("SKIP: no recent listings by candidate")
        return

    same_collection_listing = False
    for listing in recent_listings:
        listing_nft = listing.get("nft") or listing.get("asset") or {}
        if listing_nft.get("collection") == collection_slug:
            same_collection_listing = True
            break

    if REQUIRE_RECENT_LISTING and collection_slug and not same_collection_listing:
        print("SKIP: candidate has listings, but not from this collection")
        return

    collection_stats = get_collection_stats(collection_slug)

    score = score_creator(
        mint_count=mint_count,
        creator_age_days=creator_age_days,
        collection_mint_count=collection_mint_count,
        profile=profile,
        stats=collection_stats,
    )

    print(
        "PASS CANDIDATE:",
        creator,
        "| source =", creator_source,
        "| mint_count =", mint_count,
        "| collection_mints =", collection_mint_count,
        "| age_days =", creator_age_days,
        "| listings =", len(recent_listings),
        "| username =", bool(username),
        "| instagram =", bool(instagram),
        "| score =", score,
        "| min_score =", MIN_SCORE,
    )

    if score < MIN_SCORE:
        print("SKIP: score too low =", score)
        return

    safe_name = html.escape(str(name))
    safe_collection = html.escape(str(collection_name))
    safe_creator = html.escape(str(creator))

    nft_url = f"https://opensea.io/assets/{chain}/{contract}/{token_id}"
    creator_url = f"https://opensea.io/{creator}"
    first_mint_text = first_mint.strftime("%Y-%m-%d")

    if instagram:
        instagram_url = f"https://instagram.com/{instagram.strip()}"
        safe_instagram = html.escape(instagram.strip())
        instagram_line = f'📷 Instagram: <a href="{instagram_url}">@{safe_instagram}</a>'
    else:
        instagram_line = "📷 Instagram: не найден"

    if username:
        username_line = "🌊 OpenSea: " + html.escape(str(username))
    else:
        username_line = "🌊 OpenSea: имя не найдено"

    text_message = f"""
🆕 <b>Новый художник-кандидат</b>

🎨 <b>{safe_name}</b>
🗂 <b>Коллекция:</b> {safe_collection}
👤 <b>Кошелёк:</b>
<code>{safe_creator}</code>

🔎 <b>Источник автора:</b> {html.escape(creator_source)}
🧮 <b>Mint у автора:</b> {mint_count}
🖼 <b>Mint в коллекции:</b> {collection_mint_count}
⏳ <b>Возраст автора:</b> {creator_age_days} дней
📅 <b>Первый mint:</b> {first_mint_text}
📈 <b>Score:</b> {score}/100

{username_line}
{instagram_line}

👤 <a href="{creator_url}">Открыть автора на OpenSea</a>
🖼 <a href="{nft_url}">Открыть NFT</a>
""".strip()

    send_telegram(text_message)


def check_configuration():
    missing = []

    if not OPENSEA_API_KEY:
        missing.append("OPENSEA_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError("Missing variables: " + ", ".join(missing))


def main():
    check_configuration()

    print("OpenSea Fresh Artist Monitor started — STRICT MODE")

    send_telegram(
        "<b>OpenSea Fresh Artist Monitor запущен — строгий режим</b>\n"
        f"• несколько страниц свежих mint-событий\n"
        f"• сначала отсекаются массовые коллекции\n"
        f"• owner/creator коллекции имеет приоритет над получателем mint\n"
        f"• не больше {MAX_MINTS} mint у кошелька\n"
        f"• не больше {MAX_COLLECTION_MINTS} mint в коллекции\n"
        f"• первый mint не старше {MAX_CREATOR_AGE_DAYS} дней\n"
        f"• нужен свежий листинг за последние {RECENT_LISTING_DAYS} дней\n"
        f"• Instagram обязателен: {'да' if REQUIRE_INSTAGRAM else 'нет'}\n"
        "• массовые/служебные NFT отсекаются"
    )

    while True:
        try:
            events = get_recent_mint_events()

            print(
                datetime.now().strftime("%H:%M:%S"),
                "fresh mint events:",
                len(events),
            )
            print_discovery_diagnostics(events)

            for event in events:
                try:
                    process_event(event)
                except Exception as event_error:
                    # Ошибка одного события не должна останавливать весь цикл.
                    print("EVENT ERROR:", repr(event_error))

        except Exception as error:
            print("LOOP ERROR:", repr(error))

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
