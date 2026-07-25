import os
import asyncio
import logging
import feedparser
import re
import httpx
import html
import random
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from database import Database

load_dotenv()

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
CONCURRENT_LIMIT = 5 # Reduced to avoid IP bans

# AI Config
REQUESTY_API_KEY = os.getenv("REQUESTY_API_KEY", "").strip()
REQUESTY_BASE_URL = os.getenv("REQUESTY_BASE_URL", "https://api.17.wtf/v1").strip().rstrip('/')
REQUESTY_MODEL = os.getenv("REQUESTY_MODEL", "posiden/deepseek-v4-flash").strip()
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()

# Fresh & Working Nitter Instances (Updated)
RSS_SOURCES = [
    "https://nitter.net/{username}/rss",
    "https://nitter.privacydev.net/{username}/rss",
    "https://nitter.perennialte.ch/{username}/rss",
    "https://nitter.no-logs.com/{username}/rss",
    "https://xcancel.com/{username}/rss",
    "https://rsshub.rssforever.com/twitter/user/{username}"
]

# --- Helpers ---
def clean_username(raw: str) -> str:
    raw = (raw or "").strip()
    raw = raw.replace("https://", "").replace("http://", "")
    for d in ["x.com/", "twitter.com/", "nitter.net/", "xcancel.com/", "uni-sonia.com/", "nitter.perennialte.ch/"]:
        raw = raw.replace(d, "")
    return raw.lstrip("@").split("?")[0].split("/")[0].lower().strip()

def is_valid_twitter(username: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]{1,15}$", username))

def extract_id(entry):
    """Deep search for Tweet ID in RSS entry"""
    # 1. Search in various keys
    for key in ["id", "guid", "link"]:
        val = str(entry.get(key, ""))
        # Standard status link
        m = re.search(r"status(?:es)?/(\d+)", val)
        if m: return m.group(1)
        # Sequence of 17+ digits (Tweet IDs are 18-19 digits now)
        m2 = re.search(r"(\d{17,})", val)
        if m2: return m2.group(1)
    
    # 2. Search in content/summary
    content = entry.get("description", "") or entry.get("summary", "")
    m3 = re.search(r"status/(\d+)", content)
    if m3: return m3.group(1)
    
    return None

def extract_image_url(entry):
    desc = entry.get('description', '')
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
    if img_match: return img_match.group(1)
    if 'media_content' in entry and entry.media_content:
        return entry.media_content[0]['url']
    return None

def convert_to_x_link(link: str, tweet_id: str = None) -> str:
    if tweet_id:
        return f"https://x.com/i/status/{tweet_id}"
    if not link: return ""
    m = re.search(r"status/(\d+)", link)
    if m: return f"https://x.com/i/status/{m.group(1)}"
    return link.replace("nitter.net", "x.com").replace("xcancel.com", "x.com")

def persian_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z\u0600-\u06FF]", text or "")
    if not letters: return 0.0
    return len(re.findall(r"[\u0600-\u06FF]", text or "")) / len(letters)

async def translate_text(text: str) -> str:
    if not TRANSLATE_FA or not text or persian_ratio(text) > 0.5:
        return ""
    if REQUESTY_API_KEY:
        try:
            base = REQUESTY_BASE_URL if "/v1" in REQUESTY_BASE_URL else f"{REQUESTY_BASE_URL}/v1"
            payload = {
                "model": REQUESTY_MODEL,
                "messages": [{"role": "user", "content": f"Translate to colloquial Persian (informal). Keep crypto terms English: {text[:1000]}"}],
                "temperature": 0.2
            }
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {REQUESTY_API_KEY}"}, json=payload)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
        except: pass
    try:
        from deep_translator import GoogleTranslator
        return await asyncio.to_thread(GoogleTranslator(source='auto', target='fa').translate, text[:1500])
    except: return ""

async def fetch_feed(username, semaphore):
    async with semaphore:
        await asyncio.sleep(random.uniform(2, 4))
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200 or "uni-sonia" in str(resp.url) or "google.com" in str(resp.url):
                        continue
                    
                    feed = feedparser.parse(resp.text)
                    # FILTER: Check if entries are real tweets (must have an ID)
                    real_entries = [e for e in feed.entries if extract_id(e)]
                    if real_entries:
                        logger.info(f"✅ Success: @{username} from {url}")
                        return real_entries
            except: continue
        return []

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    raw_input = " ".join(context.args)
    all_usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    wait_msg = await update.message.reply_text(f"⏳ Processing {len(all_usernames)} accounts...")
    
    added = []
    for u in all_usernames:
        if is_valid_twitter(u) and not db.is_subscribed(chat_id, u):
            db.add_subscription(chat_id, u, "")
            added.append(f"@{u}")
    await wait_msg.edit_text(f"🔹 Tracking started for: {', '.join(added) if added else 'None'}")

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    username = clean_username(context.args[0])
    wait = await update.message.reply_text(f"🧪 Fetching latest tweet for @{username}...")
    entries = await fetch_feed(username, asyncio.Semaphore(1))
    if entries:
        await process_single_tweet(update.effective_chat.id, username, entries[0], context.application.bot, force=True)
        await wait.delete()
    else:
        await wait.edit_text("❌ No valid tweets found. Twitter might be blocking the request.")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    raw_input = " ".join(context.args)
    usernames = [clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]
    chat_id = str(update.effective_chat.id)
    removed = [f"@{u}" for u in usernames if db.is_subscribed(chat_id, u)]
    for u in usernames: db.remove_subscription(chat_id, u)
    await update.message.reply_text(f"🗑 Removed: {', '.join(removed) if removed else 'None'}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in db.get_all_tracked() if db.is_subscribed(chat_id, u)]
    msg = f"📋 <b>Tracking ({len(my_users)}):</b>\n\n" + ("\n".join(my_users) if my_users else "Empty")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# --- Engine ---
async def process_single_tweet(chat_id, username, entry, bot, force=False):
    tid = extract_id(entry)
    if not tid or (not force and db.is_duplicate(chat_id, tid)): return
    try:
        title = entry.get("title", "")
        translation = await translate_text(title)
        img_url = extract_image_url(entry)
        hidden_img = f'<a href="{img_url}">&#8205;</a>' if img_url else ""
        x_link = convert_to_x_link(entry.get('link', ''), tid)
        
        safe_name = html.escape(username)
        body = f"<blockquote expandable>{html.escape(title[:1900])}</blockquote>"
        text_msg = f"{hidden_img}👤 <b>@{safe_name}</b>\n{body}"
        if translation:
            text_msg += f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>Translate:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=x_link)]])
        await bot.send_message(chat_id=chat_id, text=text_msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        db.save_tweet_content(username, title, translation, img_url, x_link)
        db.mark_sent(chat_id, tid)
    except Exception as e:
        
