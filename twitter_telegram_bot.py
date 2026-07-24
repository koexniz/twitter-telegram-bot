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
CONCURRENT_LIMIT = 8

# Requesty AI Config
REQUESTY_API_KEY = os.getenv("REQUESTY_API_KEY", "").strip()
REQUESTY_BASE_URL = os.getenv("REQUESTY_BASE_URL", "https://router.requesty.ai/v1").strip().rstrip('/')
REQUESTY_MODEL = os.getenv("REQUESTY_MODEL", "tencent/hy3").strip()
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()

RSS_SOURCES = [
    "https://xcancel.com/{username}/rss",
    "https://nitter.privacydev.net/{username}/rss",
    "https://nitter.perennialte.ch/{username}/rss",
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
    link = entry.get("link", "")
    m = re.search(r"status/(\d+)", link)
    if m: return m.group(1)
    guid = entry.get("id", "")
    m = re.search(r"(\d{15,})", guid)
    return m.group(1) if m else None

def convert_to_x_link(link: str) -> str:
    if not link: return ""
    link = link.split('#')[0]
    domains = ["nitter", "xcancel", "twitter", "rsshub"]
    for d in domains:
        if d in link:
            m = re.search(r"status/(\d+)", link)
            if m: return f"https://x.com/i/status/{m.group(1)}"
    return link

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
            full_url = f"{base}/chat/completions"
            payload = {
                "model": REQUESTY_MODEL,
                "messages": [{"role": "user", "content": f"Translate this tweet to colloquial Persian. Keep crypto terms English: {text[:1000]}"}],
                "temperature": 0.2
            }
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.post(full_url, headers={"Authorization": f"Bearer {REQUESTY_API_KEY}"}, json=payload)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
        except: pass
    try:
        from deep_translator import GoogleTranslator
        return await asyncio.to_thread(GoogleTranslator(source='auto', target='fa').translate, text[:1500])
    except: return ""

async def fetch_feed(username, semaphore):
    async with semaphore:
        await asyncio.sleep(random.uniform(1.5, 3))
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200 or "uni-sonia" in str(resp.url) or "google.com" in str(resp.url):
                        continue
                    feed = feedparser.parse(resp.text)
                    if feed.entries:
                        logger.info(f"✅ Success: @{username}")
                        return feed.entries
            except: continue
        return []

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # همان کد قبلی با قابلیت Batching
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
    await wait_msg.edit_text(f"🔹 Added: {', '.join(added) if added else 'None'}")

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور جدید برای تست زنده ارسال و ترجمه"""
    if not context.args:
        await update.message.reply_text("Usage: /test username")
        return
    username = clean_username(context.args[0])
    wait = await update.message.reply_text(f"🧪 Testing @{username}...")
    sem = asyncio.Semaphore(1)
    entries = await fetch_feed(username, sem)
    if entries:
        await process_single_tweet(update.effective_chat.id, username, entries[0], context.application.bot, force=True)
        await wait.delete()
    else:
        await wait.edit_text("❌ Could not fetch feed for this user.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in db.get_all_tracked() if db.is_subscribed(chat_id, u)]
    await update.message.reply_text(f"📋 Your List ({len(my_users)}):\n\n" + "\n".join(my_users), parse_mode=ParseMode.HTML)

# --- Worker ---
async def process_single_tweet(chat_id, username, entry, bot, force=False):
    tid = extract_id(entry)
    if not tid: return
    if not force and db.is_duplicate(chat_id, tid): return
    
    try:
        title = entry.get("title", "")
        translation = await translate_text(title)
        x_link = convert_to_x_link(entry.get('link', ''))
        
        safe_name = html.escape(username)
        body = f"<blockquote expandable>{html.escape(title[:1900])}</blockquote>"
        text_msg = f"👤 <b>@{safe_name}</b>\n{body}"
        if translation:
            text_msg += f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>Translate:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=x_link)]])
        await bot.send_message(chat_id=chat_id, text=text_msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        db.mark_sent(chat_id, tid)
        logger.info(f"🚀 Sent @{username} to {chat_id}")
    except Exception as e:
        logger.error(f"Error: {e}")

async def process_user(username, last_id, sem, bot):
    entries = await fetch_feed(username, sem)
    if not entries: return
    new_last_id = last_id
    for entry in reversed(entries[:3]):
        tid = extract_id(entry)
        if not tid or tid == last_id: continue
        
        cids = db.get_subs_for_user(username)
        for cid in cids:
            await process_single_tweet(cid, username, entry, bot)
        new_last_id = tid
    if new_last_id != last_id: db.update_last_id(username, new_last_id)

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    if not tracked: return
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    await asyncio.gather(*[process_user(u, li, sem, context.application.bot) for u, li in tracked])

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("list", cmd_list))
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
