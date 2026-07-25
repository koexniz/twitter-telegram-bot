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
CONCURRENT_LIMIT = 5

# AI Config
REQUESTY_API_KEY = os.getenv("REQUESTY_API_KEY", "").strip()
REQUESTY_BASE_URL = os.getenv("REQUESTY_BASE_URL", "https://api.17.wtf/v1").strip().rstrip('/')
REQUESTY_MODEL = os.getenv("REQUESTY_MODEL", "gpt-5.5").strip()
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()

RSS_SOURCES = [
    "https://xcancel.com/{username}/rss",
    "https://nitter.privacydev.net/{username}/rss",
    "https://nitter.perennialte.ch/{username}/rss",
    "https://nitter.net/{username}/rss",
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
    for key in ["id", "guid", "link"]:
        val = str(entry.get(key, ""))
        m = re.search(r"status(?:es)?/(\d+)", val)
        if m: return m.group(1)
        m2 = re.search(r"(\d{17,})", val)
        if m2: return m2.group(1)
    return None

def extract_image_url(entry):
    """Aggressive image extraction from RSS description and media tags"""
    # 1. Check description for <img> tag (common in Nitter)
    desc = entry.get('description', '') or entry.get('summary', '')
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.I)
    if img_match:
        img_url = img_match.group(1)
        if not img_url.startswith('/'): return img_url # Skip relative paths
    
    # 2. Check media content
    if 'media_content' in entry:
        for media in entry.media_content:
            if 'url' in media: return media['url']
            
    # 3. Check enclosures
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if 'image' in enc.get('type', ''): return enc.get('href')
            
    return None

def convert_to_x_link(tid: str) -> str:
    return f"https://x.com/i/status/{tid}" if tid else ""

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
            payload = {"model": REQUESTY_MODEL, "messages": [{"role": "user", "content": f"Translate to colloquial Persian. Keep crypto terms English: {text[:1000]}"}], "temperature": 0.2}
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.post(base + "/chat/completions", headers={"Authorization": f"Bearer {REQUESTY_API_KEY}"}, json=payload)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
        except: pass
    try:
        from deep_translator import GoogleTranslator
        return await asyncio.to_thread(GoogleTranslator(source='auto', target='fa').translate, text[:1500])
    except: return ""

async def fetch_feed(username, semaphore):
    async with semaphore:
        await asyncio.sleep(random.uniform(1, 2))
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200 or "uni-sonia" in str(resp.url) or "google.com" in str(resp.url): continue
                    feed = feedparser.parse(resp.text)
                    real_entries = [e for e in feed.entries if extract_id(e)]
                    if real_entries:
                        logger.info(f"✅ Success: @{username}")
                        return real_entries
            except: continue
        return []

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    raw_input = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    wait = await update.message.reply_text(f"⏳ Processing {len(usernames)} accounts...")
    added = []
    for u in usernames:
        if is_valid_twitter(u) and not db.is_subscribed(chat_id, u):
            db.add_subscription(chat_id, u, "")
            added.append(f"@{u}")
    await wait.edit_text(f"🔹 Tracking: {', '.join(added) if added else 'None'}")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
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
    tracked = db.get_all_tracked()
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in tracked if db.is_subscribed(chat_id, u)]
    msg = f"📋 <b>Your List ({len(my_users)}):</b>\n\n" + ("\n".join(my_users) if my_users else "Empty")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    username = clean_username(context.args[0])
    wait = await update.message.reply_text(f"🧪 Testing @{username}...")
    entries = await fetch_feed(username, asyncio.Semaphore(1))
    if entries:
        await process_single_tweet(update.effective_chat.id, username, entries[0], context.application.bot, force=True)
        await wait.delete()
    else:
        await wait.edit_text("❌ No valid tweets found.")

# --- Engine ---
async def process_single_tweet(chat_id, username, entry, bot, force=False):
    tid = extract_id(entry)
    if not tid or (not force and db.is_duplicate(chat_id, tid)): return
    try:
        title = entry.get("title", "")
        translation = await translate_text(title)
        img_url = extract_image_url(entry)
        x_link = convert_to_x_link(tid)
        
        safe_name = html.escape(username)
        body = f"👤 <b>@{safe_name}</b>\n<blockquote expandable>{html.escape(title[:1900])}</blockquote>"
        if translation:
            body += f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>Translate:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=x_link)]])

        # --- SMART MEDIA SENDING ---
        if img_url:
            # If text is short, send as caption
            if len(body) < 1000:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=img_url,
                    caption=body,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML
                )
            else:
                # If text is long, send photo first, then text
                photo_msg = await bot.send_photo(chat_id=chat_id, photo=img_url)
                await bot.send_message(
                    chat_id=chat_id,
                    text=body,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=photo_msg.message_id
                )
        else:
            # No image, send text only
            await bot.send_message(
                chat_id=chat_id,
                text=body,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

        db.save_tweet_content(username, title, translation, img_url, x_link)
        db.mark_sent(chat_id, tid)
        logger.info(f"🚀 Sent @{username} (with Photo: {'Yes' if img_url else 'No'})")
        
    except Exception as e:
        # Fallback: if photo fails, send text only
        logger.error(f"Media Send Error: {e}")
        try:
            await bot.send_message(chat_id=chat_id, text=body, reply_markup=kb, parse_mode=ParseMode.HTML)
        except: pass

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

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    if not tracked: return
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    for u, li in tracked:
        asyncio.create_task(process_user(u, li, sem, context.application.bot))

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 Active.")))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("test", cmd_test))
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
