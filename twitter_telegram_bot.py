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
CONCURRENT_LIMIT = 8

# AI Config (DeepSeek / Requesty)
REQUESTY_API_KEY = os.getenv("REQUESTY_API_KEY", "").strip()
REQUESTY_BASE_URL = os.getenv("REQUESTY_BASE_URL", "https://api.17.wtf/v1").strip().rstrip('/')
REQUESTY_MODEL = os.getenv("REQUESTY_MODEL", "posiden/deepseek-v4-flash").strip()
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
    """Robust tweet ID extraction from various RSS formats"""
    # 1. Try to find in link
    link = entry.get("link", "")
    m = re.search(r"status(?:es)?/(\d+)", link)
    if m: return m.group(1)
    
    # 2. Try to find in guid/id
    guid = entry.get("id", "") or entry.get("guid", "")
    m = re.search(r"(\d{15,})", str(guid))
    if m: return m.group(1)
    
    # 3. Try to find in description (some Nitter instances hide it there)
    desc = entry.get("description", "")
    m = re.search(r"status/(\d+)", desc)
    if m: return m.group(1)
    
    return None

def extract_image_url(entry):
    desc = entry.get('description', '')
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
    if img_match: return img_match.group(1)
    if 'media_content' in entry and entry.media_content:
        return entry.media_content[0]['url']
    return None

def convert_to_x_link(link: str) -> str:
    if not link: return ""
    link = link.split('#')[0]
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
            # Force /v1 if missing in URL
            base = REQUESTY_BASE_URL.strip().rstrip('/')
            if not base.endswith('/v1'):
                base += '/v1'
            
            full_url = f"{base}/chat/completions"
            payload = {
                "model": REQUESTY_MODEL,
                "messages": [{"role": "user", "content": f"Translate to colloquial Persian (Tehran dialect). Keep crypto terms English: {text[:1000]}"}],
                "temperature": 0.2
            }
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.post(full_url, headers={"Authorization": f"Bearer {REQUESTY_API_KEY}"}, json=payload)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                else:
                    logger.warning(f"AI Error: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.warning(f"AI Conn Error: {e}")

    # Fallback to Google
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
    if not context.args: return
    raw_input = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    wait_msg = await update.message.reply_text(f"⏳ Processing {len(usernames)} accounts...")
    added = []
    for u in usernames:
        if is_valid_twitter(u) and not db.is_subscribed(chat_id, u):
            db.add_subscription(chat_id, u, "")
            added.append(f"@{u}")
    await wait_msg.edit_text(f"🔹 Added: {', '.join(added) if added else 'None'}")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    raw_input = " ".join(context.args)
    usernames = [clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]
    chat_id = str(update.effective_chat.id)
    removed = []
    for u in usernames:
        if db.is_subscribed(chat_id, u):
            db.remove_subscription(chat_id, u)
            removed.append(f"@{u}")
    await update.message.reply_text(f"🗑 Removed: {', '.join(removed) if removed else 'None'}")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in db.get_all_tracked() if db.is_subscribed(chat_id, u)]
    msg = f"📋 Your Tracking List ({len(my_users)}):\n\n" + ("\n".join(my_users) if my_users else "Empty")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: /test username")
        return

    username = clean_username(context.args[0])
    chat_id = update.effective_chat.id
    wait_msg = await update.message.reply_text(f"🧪 Testing @{username}...")
    
    try:
        entries = await fetch_feed(username, asyncio.Semaphore(1))
        if not entries:
            await wait_msg.edit_text(f"❌ Could not fetch any tweets for @{username}.")
            return

        latest_entry = entries[0]
        tid = extract_id(latest_entry)
        
        # اگر آیدی پیدا نشد، به کاربر بگو تا بفهمیم مشکل از کجاست
        if not tid:
            await wait_msg.edit_text(f"❌ Found tweets, but could not extract Tweet ID for @{username}. Format changed?")
            return

        # ارسال توییت
        await process_single_tweet(chat_id, username, latest_entry, context.application.bot, force=True)
        await wait_msg.delete()
        
    except Exception as e:
        logger.error(f"Test Error: {e}")
        await wait_msg.edit_text(f"❌ Test failed: {str(e)}")

# --- Background Engine ---
async def process_single_tweet(chat_id, username, entry, bot, force=False):
    tid = extract_id(entry)
    if not tid or (not force and db.is_duplicate(chat_id, tid)): return
    try:
        title = entry.get("title", "")
        translation = await translate_text(title)
        img_url = extract_image_url(entry)
        hidden_img = f'<a href="{img_url}">&#8205;</a>' if img_url else ""
        x_link = convert_to_x_link(entry.get('link', ''))
        
        safe_name = html.escape(username)
        body = f"<blockquote expandable>{html.escape(title[:1900])}</blockquote>"
        text_msg = f"{hidden_img}👤 <b>@{safe_name}</b>\n{body}"
        
        if translation:
            text_msg += f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>Translate:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=x_link)]])
        await bot.send_message(chat_id=chat_id, text=text_msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        
        # ✅ SAVING TO DATABASE FOR MOBILE APP (Correct Placement)
        db.save_tweet_content(username, title, translation, img_url, x_link)
        
        db.mark_sent(chat_id, tid)
        logger.info(f"🚀 Sent @{username} to {chat_id}")
    except Exception as e:
        logger.error(f"Send Error: {e}")

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
    tasks = [process_user(u, li, sem, context.application.bot) for u, li in tracked]
    await asyncio.gather(*tasks)

def main():
    if not TOKEN: return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 Bot is active.")))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("test", cmd_test))
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
