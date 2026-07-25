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

# --- Config ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
CONCURRENT_LIMIT = 3 # Low limit to prevent IP Ban

# AI Config
REQUESTY_API_KEY = os.getenv("REQUESTY_API_KEY", "").strip()
REQUESTY_BASE_URL = os.getenv("REQUESTY_BASE_URL", "https://api.17.wtf/v1").strip().rstrip('/')
REQUESTY_MODEL = os.getenv("REQUESTY_MODEL", "posiden/deepseek-v4-flash").strip()
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()

# High Stability Sources
RSS_SOURCES = [
    "https://nitter.privacydev.net/{username}/rss",
    "https://xcancel.com/{username}/rss",
    "https://nitter.net/{username}/rss",
    "https://nitter.no-logs.com/{username}/rss",
    "https://rsshub.rssforever.com/twitter/user/{username}"
]

# --- Helpers ---
def clean_username(raw: str) -> str:
    raw = (raw or "").strip()
    raw = raw.replace("https://", "").replace("http://", "")
    for d in ["x.com/", "twitter.com/", "nitter.net/", "xcancel.com/", "uni-sonia.com/"]:
        raw = raw.replace(d, "")
    return raw.lstrip("@").split("?")[0].split("/")[0].lower().strip()

def is_valid_twitter(username: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]{1,15}$", username))

def extract_id(entry):
    """Extra robust ID detection"""
    for key in ["id", "guid", "link"]:
        val = str(entry.get(key, ""))
        m = re.search(r"status(?:es)?/(\d+)", val)
        if m: return m.group(1)
        m2 = re.search(r"(\d{17,})", val)
        if m2: return m2.group(1)
    return None

def extract_image_url(entry):
    desc = entry.get('description', '') or entry.get('summary', '')
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.I)
    if img_match:
        img_url = img_match.group(1)
        if "twimg.com" in img_url or img_url.startswith("http"): return img_url
    if 'media_content' in entry and entry.media_content: return entry.media_content[0]['url']
    return None

async def translate_text(text: str) -> str:
    if not TRANSLATE_FA or not text or persian_ratio(text) > 0.5: return ""
    if REQUESTY_API_KEY:
        try:
            base = REQUESTY_BASE_URL if "/v1" in REQUESTY_BASE_URL else f"{REQUESTY_BASE_URL}/v1"
            payload = {"model": REQUESTY_MODEL, "messages": [{"role": "user", "content": f"Translate to colloquial Persian. Keep crypto terms English: {text[:800]}"}], "temperature": 0.2}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(base + "/chat/completions", headers={"Authorization": f"Bearer {REQUESTY_API_KEY}"}, json=payload)
                if resp.status_code == 200: return resp.json()["choices"][0]["message"]["content"].strip()
        except: pass
    try:
        from deep_translator import GoogleTranslator
        return await asyncio.to_thread(GoogleTranslator(source='auto', target='fa').translate, text[:1200])
    except: return ""

async def fetch_feed(username, semaphore):
    async with semaphore:
        await asyncio.sleep(random.uniform(3, 6)) # High delay to avoid bans
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200 or "google.com" in str(resp.url) or "uni-sonia" in str(resp.url):
                        continue
                    feed = feedparser.parse(resp.text)
                    valid_entries = [e for e in feed.entries if extract_id(e)]
                    if valid_entries:
                        logger.info(f"✅ Success: @{username}")
                        return valid_entries
            except: continue
        return []

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_input = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    added = []
    for u in usernames:
        if is_valid_twitter(u) and not db.is_subscribed(chat_id, u):
            db.add_subscription(chat_id, u, "")
            added.append(f"@{u}")
    await update.message.reply_text(f"🔹 Started monitoring: {', '.join(added) if added else 'None'}")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    removed = []
    for arg in context.args:
        u = clean_username(arg)
        if db.is_subscribed(chat_id, u):
            db.remove_subscription(chat_id, u)
            removed.append(f"@{u}")
    await update.message.reply_text(f"🗑 Removed: {', '.join(removed) if removed else 'None'}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in db.get_all_tracked() if db.is_subscribed(chat_id, u)]
    await update.message.reply_text(f"📋 <b>List ({len(my_users)}):</b>\n\n" + "\n".join(my_users), parse_mode=ParseMode.HTML)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = clean_username(context.args[0]) if context.args else "ElonMusk"
    wait = await update.message.reply_text(f"🧪 Testing @{username}...")
    entries = await fetch_feed(username, asyncio.Semaphore(1))
    if entries:
        await process_single_tweet(update.effective_chat.id, username, entries[0], context.application.bot, force=True)
        await wait.delete()
    else:
        await wait.edit_text("❌ No valid tweets found. Source might be blocked.")

# --- Engine ---
async def process_single_tweet(chat_id, username, entry, bot, force=False):
    tid = extract_id(entry)
    if not tid or (not force and db.is_duplicate(chat_id, tid)): return
    try:
        title = entry.get("title", "")
        translation = await translate_text(title)
        img_url = extract_image_url(entry)
        x_link = f"https://x.com/i/status/{tid}"
        
        text = f"👤 <b>@{html.escape(username)}</b>\n<blockquote expandable>{html.escape(title[:1900])}</blockquote>"
        if translation:
            text += f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>Translate:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=x_link)]])
        
        if img_url:
            try:
                await bot.send_photo(chat_id=chat_id, photo=img_url, caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except:
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        
        db.save_tweet_content(username, title, translation, img_url, x_link)
        db.mark_sent(chat_id, tid)
    except Exception as e:
        logger.error(f"Error: {e}")

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    tasks = [process_user(u, li, sem, context.application.bot) for u, li in tracked]
    await asyncio.gather(*tasks)

async def process_user(username, last_id, sem, bot):
    entries = await fetch_feed(username, sem)
    if not entries: return
    new_last_id = last_id
    for entry in reversed(entries[:3]):
        tid = extract_id(entry)
        if not tid or tid == last_id: continue
        for cid in db.get_subs_for_user(username):
            await process_single_tweet(cid, username, entry, bot)
        new_last_id = tid
    if new_last_id != last_id: db.update_last_id(username, new_last_id)

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("test", cmd_test))
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
