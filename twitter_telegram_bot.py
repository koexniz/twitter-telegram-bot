import os
import json
import asyncio
import logging
import feedparser
import html
import re
import socket
import httpx
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

load_dotenv()

# =============================
# Config
# =============================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_CHAT_ID", "").split(",") if x.strip()]

DATA_FILE = os.path.join(DATA_DIR, "tracked_users.json")
FILTERS_FILE = os.path.join(DATA_DIR, "filters.json")
SENT_IDS_FILE = os.path.join(DATA_DIR, "sent_ids.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))
MAX_BACKFILL_ON_MISSING_LAST_ID = int(os.getenv("MAX_BACKFILL_ON_MISSING_LAST_ID", "5"))
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes", "on")
TRANSLATE_CACHE_MAX = int(os.getenv("TRANSLATE_CACHE_MAX", "1500"))
DEDUP_MAX_PER_CHAT = int(os.getenv("DEDUP_MAX_PER_CHAT", "2000"))
DEDUP_FILE_MAX_PER_KEY = int(os.getenv("DEDUP_FILE_MAX_PER_KEY", "500"))
FOLD_THRESHOLD = int(os.getenv("FOLD_THRESHOLD", "280"))
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "21600"))

RSS_SOURCES = [
    "https://nitter.privacydev.net/{username}/rss",
    "https://nitter.no-name-given.com/{username}/rss",
    "https://nitter.freedit.eu/{username}/rss",
]
NITTER_DOMAINS = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.no-name-given.com",
    "https://nitter.hu",
]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

socket.setdefaulttimeout(HTTP_TIMEOUT)
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# =============================
# DEDUP
# =============================
_dedup_ram: Dict[str, Set[str]] = {}

def _dedup_key(chat_id: Any) -> str:
    return str(chat_id)

def _load_sent_ids() -> Dict[str, List[str]]:
    if os.path.exists(SENT_IDS_FILE):
        try:
            with open(SENT_IDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"sent_ids.json load failed: {e}")
    return {}

def _save_sent_ids_file(data: Dict[str, List[str]]) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SENT_IDS_FILE)), exist_ok=True)
        with open(SENT_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"sent_ids.json save failed: {e}")

def _init_dedup() -> None:
    data = _load_sent_ids()
    for key, ids in data.items():
        if isinstance(ids, list):
            _dedup_ram[key] = set(ids[-DEDUP_MAX_PER_CHAT:])
    total = sum(len(v) for v in _dedup_ram.values())
    logger.info(f"Dedup init: {total} IDs for {len(_dedup_ram)} chats")

def is_already_sent(chat_id: Any, tweet_id: str) -> bool:
    if not tweet_id or not re.match(r"^\d+$", str(tweet_id)):
        return False
    return tweet_id in _dedup_ram.get(_dedup_key(chat_id), set())

def mark_as_sent(chat_id: Any, tweet_id: str) -> None:
    if not tweet_id or not re.match(r"^\d+$", str(tweet_id)):
        return
    key = _dedup_key(chat_id)
    _dedup_ram.setdefault(key, set()).add(tweet_id)

def _flush_dedup_to_file() -> None:
    data: Dict[str, List[str]] = {}
    for key, ids in _dedup_ram.items():
        lst = list(ids)
        data[key] = lst[-DEDUP_FILE_MAX_PER_KEY:] if len(lst) > DEDUP_FILE_MAX_PER_KEY else lst
    _save_sent_ids_file(data)

# =============================
# Translation Engine
# =============================
AEROLINK_API_KEY = os.getenv("AEROLINK_API_KEY", "").strip()
AEROLINK_BASE_URL = os.getenv("AEROLINK_BASE_URL", "").rstrip("/")
AEROLINK_MODEL = os.getenv("AEROLINK_MODEL", "gpt-4o-mini").strip()

translate_cache: Dict[str, str] = {}

def normalize_tweet_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u200f", "").replace("\u200e", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def persian_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z\u0600-\u06FF]", text or "")
    if not letters:
        return 0.0
    return len(re.findall(r"[\u0600-\u06FF]", text or "")) / len(letters)

def translate_fa(text: str) -> Optional[str]:
    if not TRANSLATE_FA:
        return None
    cleaned = normalize_tweet_text(text)
    if not cleaned or persian_ratio(cleaned) > 0.55:
        return None
    if cleaned in translate_cache:
        return translate_cache[cleaned]

    result = None
    if AEROLINK_API_KEY and AEROLINK_BASE_URL:
        try:
            url = f"{AEROLINK_BASE_URL}/chat/completions"
            headers = {"Authorization": f"Bearer {AEROLINK_API_KEY}", "Content-Type": "application/json"}
            prompt = (
                "You are an expert Persian crypto influencer.\n"
                "Translate this English tweet into natural, informal (Tehran dialect) Persian for a crypto channel.\n"
                "Keep crypto terms in English: Airdrop, Mainnet, Testnet, Mint, Stake, Snapshot, Whitelist, Listing, etc.\n"
                "Keep all @usernames, #hashtags, and links untouched.\n"
                f"Text: {cleaned}"
            )
            payload = {"model": AEROLINK_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
            with httpx.Client(timeout=8.0) as client:
                res = client.post(url, headers=headers, json=payload)
                if res.status_code == 200:
                    result = res.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Aerolink translate error: {e}")

    if not result:
        try:
            from deep_translator import GoogleTranslator
            result = GoogleTranslator(source="auto", target="fa").translate(cleaned[:4500])
        except Exception:
            pass

    if result and len(translate_cache) < TRANSLATE_CACHE_MAX:
        translate_cache[cleaned] = result
    return result

# =============================
# Storage
# =============================
def default_filters() -> Dict[str, Any]:
    return {"global": {"filter_rt": True, "filter_replies": True}, "chats": {}}

def load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return deepcopy(default)
    return deepcopy(default)

def save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def clean_username(raw: str) -> str:
    raw = (raw or "").strip()
    raw = raw.replace("https://", "").replace("http://", "").replace("www.", "").lstrip("@")
    for domain in ("x.com/", "twitter.com/", "nitter.net/", "xcancel.com/"):
        if domain in raw.lower():
            raw = raw.lower().split(domain, 1)[-1]
            break
    return raw.split("?")[0].split("#")[0].split("/")[0].lower().strip()

def valid_username(username: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]{1,15}$", username or ""))

tracked: Dict[str, Dict[str, Any]] = load_json(DATA_FILE, {})
filters_db: Dict[str, Any] = load_json(FILTERS_FILE, default_filters())
_init_dedup()

def save_tracked() -> None: save_json(DATA_FILE, tracked)
def save_filters() -> None: save_json(FILTERS_FILE, filters_db)

def get_chat_filters(chat_id: Any) -> Dict[str, Any]:
    chat_key = str(chat_id)
    filters_db.setdefault("chats", {})
    if chat_key not in filters_db["chats"]:
        filters_db["chats"][chat_key] = {"keywords": [], "alert_keywords": [], "filter_rt": True, "filter_replies": True}
    return filters_db["chats"][chat_key]

def chat_has_username(chat_id: Any, username: str) -> bool:
    return username in tracked and any(str(chat_id) == str(c) for c in tracked[username].get("chats", []))

def add_chat_to_username(chat_id: Any, username: str, last_id: str) -> None:
    if username not in tracked:
        tracked[username] = {"last_id": str(last_id), "chats": []}
    if str(chat_id) not in [str(c) for c in tracked[username].get("chats", [])]:
        tracked[username]["chats"].append(chat_id)

def remove_chat_from_username(chat_id: Any, username: str) -> bool:
    if username not in tracked: return False
    old = tracked[username].get("chats", [])
    new = [c for c in old if str(c) != str(chat_id)]
    if len(new) == len(old): return False
    tracked[username]["chats"] = new
    if not new: del tracked[username]
    return True

# =============================
# Ultra-Fast Async RSS Fetcher
# =============================
async def fetch_single_source(client: httpx.AsyncClient, template: str, username: str) -> Optional[Any]:
    url = template.format(username=username)
    try:
        res = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml, */*"
            },
            follow_redirects=True,
            timeout=5.0
        )
        if res.status_code == 200 and res.content:
            feed = feedparser.parse(res.content)
            if feed and hasattr(feed, 'entries') and feed.entries:
                return feed
    except Exception:
        pass
    return None
import os
import httpx
from typing import Optional, Any

# کلید اصلی API
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "cbf5caed10msh6eef77ac9dc816fp12095bjsnfd641f9fe9c0")

# لیست اندپوینت‌های چرخشی برای جلوگیری از ۴۲۹ و قطعی
RAPID_ENDPOINTS = [
    {
        "host": "twitter32.p.rapidapi.com",
        "url": "https://twitter32.p.rapidapi.com/getUserByScreenName?screen_name={username}"
    },
    {
        "host": "twitter-v23.p.rapidapi.com",
        "url": "https://twitter-v23.p.rapidapi.com/v2/UserByScreenName/?username={username}"
    },
    {
        "host": "x-com2.p.rapidapi.com",
        "url": "https://x-com2.p.rapidapi.com/UserByScreenName/?username={username}"
    },
    {
        "host": "twitter-v24.p.rapidapi.com",
        "url": "https://twitter-v24.p.rapidapi.com/user/about?username={username}"
    }
]

async def fetch_rss_feed(username: str) -> Optional[Any]:
    username = clean_username(username)
    if not valid_username(username) or not RAPIDAPI_KEY:
        return None

    async with httpx.AsyncClient(timeout=8.0) as client:
        for ep in RAPID_ENDPOINTS:
            headers = {
                "x-rapidapi-host": ep["host"],
                "x-rapidapi-key": RAPIDAPI_KEY,
                "Content-Type": "application/json"
            }
            url = ep["url"].format(username=username)

            try:
                res = await client.get(url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    entries = []

                    user_data = data.get("data", {}).get("user", {}).get("result", {}) or data
                    timeline = user_data.get("timeline", {}) or user_data.get("tweets", [])

                    if isinstance(timeline, dict):
                        instructions = timeline.get("instructions", [])
                        for item in instructions:
                            for entry in item.get("entries", []):
                                tweet_res = entry.get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {})
                                legacy = tweet_res.get("legacy", {})
                                tid = legacy.get("id_str") or tweet_res.get("rest_id")
                                text = legacy.get("full_text") or legacy.get("text", "")

                                if tid and text:
                                    entries.append({
                                        "id": str(tid),
                                        "link": f"https://x.com/{username}/status/{tid}",
                                        "title": text,
                                        "summary": text
                                    })
                    elif isinstance(timeline, list):
                        for t in timeline:
                            tid = t.get("id_str") or t.get("id")
                            text = t.get("full_text") or t.get("text", "")
                            if tid and text:
                                entries.append({
                                    "id": str(tid),
                                    "link": f"https://x.com/{username}/status/{tid}",
                                    "title": text,
                                    "summary": text
                                })

                    if entries:
                        class DummyFeed:
                            pass
                        f = DummyFeed()
                        f.entries = entries
                        return f
            except Exception:
                continue

    return None
            # ۲. دریافت مستقیم توئیت‌ها با user_id ذخیره شده
            tweets_url = f"https://{RAPIDAPI_HOST}/user/tweetsandreplies?user_id={user_id}&limit=5"
            res = await client.get(tweets_url, headers=headers)

            if res.status_code == 200:
                tweets_data = res.json()
                instructions = tweets_data.get("instructions", []) or tweets_data.get("data", [])
                entries = []

                for item in instructions:
                    entries_list = item.get("entries", []) if isinstance(item, dict) else []
                    for entry in entries_list:
                        tweet_result = entry.get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {})
                        if not tweet_result:
                            continue
                        
                        legacy = tweet_result.get("legacy", {})
                        tweet_id = legacy.get("id_str") or tweet_result.get("rest_id")
                        text = legacy.get("full_text", "")

                        if tweet_id and text:
                            entries.append({
                                "id": str(tweet_id),
                                "link": f"https://x.com/{username}/status/{tweet_id}",
                                "title": text,
                                "summary": text
                            })

                if entries:
                    class DummyFeed:
                        pass
                    f = DummyFeed()
                    f.entries = entries
                    return f

        except Exception as e:
            print(f"RapidAPI Fetch Error for {username}: {e}")

    return None
def extract_tweet_id(entry: Any) -> str:
    for text in (entry.get("link", ""), entry.get("id", ""), entry.get("guid", "")):
        m = re.search(r"(\d{15,})", str(text))
        if m: return m.group(1)
    return ""

def escape_and_linkify(text: str) -> str:
    parts = []
    last = 0
    for m in re.finditer(r"https?://[^\s<>\"']+", text):
        parts.append(html.escape(text[last:m.start()]))
        url = m.group(0)
        short = re.sub(r"^https?://", "", url)[:25] + "…"
        parts.append(f' <a href="{html.escape(url)}">{html.escape(short)}</a>')
        last = m.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)

def build_tweet_message(username: str, title: str, fa_text: Optional[str], is_alert: bool, image_url: Optional[str] = None) -> str:
    header_emoji = "🚨" if is_alert else "📣"
    hidden_img = f'<a href="{image_url}">&#8203;</a>' if image_url else ""
    header = f"{hidden_img}{header_emoji} <b>پست جدید از دنیای کریپتو!</b>\n\n👤 <b>منبع:</b> <code>@{html.escape(username)}</code>"

    body_raw = title[:2000]
    body_linkified = escape_and_linkify(body_raw)
    sep = "➖➖➖➖➖➖➖➖➖➖"

    text = f"{header}\n\n{sep}\n\n📝 <b>متن اصلی (انگلیسی):</b>\n\n<blockquote>{body_linkified}</blockquote>"

    if fa_text and fa_text.strip() != title.strip():
        fa_escaped = html.escape(fa_text[:1200])
        text += f"\n\n{sep}\n\n🇮🇷 <b>ترجمه فارسی:</b>\n\n<blockquote>{fa_escaped}</blockquote>"

    return text[:4096]

async def send_tweet_entry(chat_id: Any, username: str, entry: Any, bot: Any) -> bool:
    title = normalize_tweet_text(entry.get("title", ""))
    title = re.sub(rf"^{re.escape(username)}\s*:\s*", "", title, flags=re.IGNORECASE)
    tweet_id = extract_tweet_id(entry)

    if is_already_sent(chat_id, tweet_id):
        return False

    link = f"https://x.com/{username}/status/{tweet_id}" if tweet_id else f"https://x.com/{username}"
    fa_text = await asyncio.to_thread(translate_fa, title) if TRANSLATE_FA else None
    
    image_url = None
    for enc in entry.get("enclosures", []) or []:
        if "image" in enc.get("type", "").lower():
            image_url = enc.get("href")
            break

    text = build_tweet_message(username, title, fa_text, False, image_url)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("View on X", url=link)]])

    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        mark_as_sent(chat_id, tweet_id)
        return True
    except Exception as e:
        logger.error(f"Send failed: {e}")
        return False

# =============================
# Commands
# =============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("🤖 **ربات مانیتورینگ توییتر فعال است.**\nبرای اضافه کردن اکانت: `/add username`", parse_mode=ParseMode.MARKDOWN)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not context.args: return
    chat_id = update.effective_chat.id
    raw_users = [clean_username(u) for u in context.args if clean_username(u)]

    msg = await update.message.reply_text(f"⏳ در حال بررسی و افزودن {len(raw_users)} اکانت...")
    added = []
    
    for username in raw_users:
        if valid_username(username) and not chat_has_username(chat_id, username):
            feed = await fetch_rss_feed(username)
            last_id = extract_tweet_id(feed.entries[0]) if feed and feed.entries else ""
            add_chat_to_username(chat_id, username, last_id)
            added.append(username)

    if added: save_tracked()
    await msg.edit_text(f"✅ تعداد {len(added)} اکانت با موفقیت اضافه شد:\n" + "\n".join(f"- `@{u}`" for u in added), parse_mode=ParseMode.MARKDOWN)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message: return
    chat_id = update.effective_chat.id
    users = [u for u, info in tracked.items() if str(chat_id) in [str(c) for c in info.get("chats", [])]]
    if not users:
        await update.message.reply_text("هیچ اکانتی در این چت ثبت نشده است.")
        return
    await update.message.reply_text("📋 **اکانت‌های فعال:**\n\n" + "\n".join(f"- `@{u}`" for u in sorted(users)), parse_mode=ParseMode.MARKDOWN)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not context.args: return
    chat_id = update.effective_chat.id
    u = clean_username(context.args[0])
    if remove_chat_from_username(chat_id, u):
        save_tracked()
        await update.message.reply_text(f"❌ اکانت `@{u}` حذف شد.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("این اکانت یافت نشد.")

# =============================
# Background Checker (High Speed)
# =============================
async def check_twitter_updates(app: Application) -> None:
    while True:
        if tracked:
            usernames = list(tracked.keys())
            BATCH_SIZE = 3  # بررسی دسته‌های ۳ تایی برای جلوگیری از Rate Limit

            for i in range(0, len(usernames), BATCH_SIZE):
                batch = usernames[i:i + BATCH_SIZE]
                feeds = await asyncio.gather(*[fetch_rss_feed(u) for u in batch], return_exceptions=True)

                for username, feed in zip(batch, feeds):
                    if isinstance(feed, Exception) or not feed or not hasattr(feed, 'entries') or not feed.entries:
                        continue

                    info = tracked.get(username, {})
                    last_id = str(info.get("last_id", ""))
                    new_entries = []

                    for entry in feed.entries:
                        tid = extract_tweet_id(entry)
                        if last_id and tid == last_id:
                            break
                        new_entries.append(entry)

                    for entry in reversed(new_entries[:MAX_BACKFILL_ON_MISSING_LAST_ID]):
                        tid = extract_tweet_id(entry)
                        if tid:
                            for chat_id in list(info.get("chats", [])):
                                await send_tweet_entry(chat_id, username, entry, app.bot)

                    if new_entries and username in tracked:
                        latest_tid = extract_tweet_id(new_entries[0])
                        if latest_tid:
                            tracked[username]["last_id"] = latest_tid
                            save_tracked()

                # وقفه ۲ ثانیه‌ای بین هر دسته ۳ تایی برای عدم لیمیت آی‌پی
                await asyncio.sleep(2.0)

        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(app: Application) -> None:
    asyncio.create_task(check_twitter_updates(app))
    await app.bot.set_my_commands([
        BotCommand("start", "شروع ربات"),
        BotCommand("add", "افزودن اکانت"),
        BotCommand("del", "حذف اکانت"),
        BotCommand("list", "لیست اکانت‌ها")
    ])

def main() -> None:
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN missing!")
        return
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.run_polling()

if __name__ == "__main__":
    main()
