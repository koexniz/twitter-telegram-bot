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
CONCURRENT_LIMIT = 5

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
    "https://nitter.net/{username}/rss"
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
    if 'enclosures' in entry:
        for enc in entry.enclosures:
            if enc.get('type', '').startswith('image/') or re.search(r'\.(jpg|jpeg|png|webp)', enc.get('href', ''), re.I):
                return enc.get('href')
    desc = entry.get('description', '') or entry.get('summary', '')
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.I)
    if img_match:
        url = img_match.group(1)
        if "/pic/media%2F" in url:
            media_id = url.split("%2F")[-1].split('?')[0]
            return f"https://pbs.twimg.com/media/{media_id}"
        return url
    return None

def convert_to_x_link(tid: str) -> str:
    return f"https://x.com/i/status/{tid}" if tid else ""

def persian_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z\u0600-\u06FF]", text or "")
    return len(re.findall(r"[\u0600-\u06FF]", text or "")) / len(letters) if letters else 0.0

async def translate_text(text: str) -> str:
    if not TRANSLATE_FA or not text or persian_ratio(text) > 0.5: return ""
    if REQUESTY_API_KEY:
        try:
            base = REQUESTY_BASE_URL if "/v1" in REQUESTY_BASE_URL else f"{REQUESTY_BASE_URL}/v1"
            payload = {"model": REQUESTY_MODEL, "messages": [{"role": "user", "content": f"Translate to colloquial Persian. Keep crypto terms English: {text[:1000]}"}], "temperature": 0.2}
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.post(base + "/chat/completions", headers={"Authorization": f"Bearer {REQUESTY_API_KEY}"}, json=payload)
                if resp.status_code == 200: return resp.json()["choices"][0]["message"]["content"].strip()
        except: pass
    try:
        from deep_translator import GoogleTranslator
        return await asyncio.to_thread(GoogleTranslator(source='auto', target='fa').translate, text[:1500])
    except: return ""

async def fetch_feed(username, semaphore):
    async with semaphore:
        await asyncio.sleep(random.uniform(2, 4))
        headers = {"User-Agent": "Mozilla/5.0"}
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=True) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200 or "uni-sonia" in str(resp.url): continue
                    feed = feedparser.parse(resp.text)
                    valid = [e for e in feed.entries if extract_id(e)]
                    if valid: return valid
            except: continue
        return []

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    raw = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw) if u]))
    chat_id = str(update.effective_chat.id)
    wait = await update.message.reply_text(f"⏳ Processing {len(usernames)} accounts...")
    added = []
    for u in usernames:
        if is_valid_twitter(u) and not db.is_subscribed(chat_id, u):
            db.add_subscription(chat_id, u, "")
            added.append(f"@{u}")
    await wait.edit_text(f"✅ Success for: {', '.join(added) if added else 'None'}")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    for arg in context.args: db.remove_subscription(chat_id, clean_username(arg))
    await update.message.reply_text("🗑 Removed.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in db.get_all_tracked() if db.is_subscribed(chat_id, u)]
    msg = f"📋 <b>Tracking ({len(my_users)}):</b>\n\n" + ("\n".join(my_users) if my_users else "Empty")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    username = clean_username(context.args[0])
    wait = await update.message.reply_text(f"🧪 Testing @{username}...")
    entries = await fetch_feed(username, asyncio.Semaphore(1))
    if entries:
        await process_single_tweet(update.effective_chat.id, username, entries[0], context.application.bot, force=True)
        await wait.delete()
    else: await wait.edit_text("❌ Feed Error.")

# --- Engine ---
async def process_single_tweet(chat_id, username, entry, bot, force=False):
    tid = extract_id(entry)
    if not tid or (not force and db.is_duplicate(chat_id, tid)): return
    try:
        title = entry.get("title", "")
        translation = await translate_text(title)
        img_url = extract_image_url(entry)
        x_link = f"https://x.com/i/status/{tid}"
        
        # --- NEW LUXURY DESIGN ---
        header = f"🔔 <b>NEW UPDATE | @{html.escape(username).upper()}</b>"
        
        # Main content with clean spacing
        body = f"\n📝 <b>Original:</b>\n<blockquote expandable>{html.escape(title[:1900])}</blockquote>"
        
        text_msg = f"{header}\n{body}"
        
        if translation:
            # Better divider and Persian styling
            divider = "\n" + "━" * 15 
            text_msg += f"{divider}\n🇮🇷 <b>Persian Translation:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
        
        # Inline buttons with icons
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Open in X", url=x_link),
            InlineKeyboardButton("📱 Mobile App", url="https://resilient-respect-production.up.railway.app")
        ]])

        if img_url:
            try:
                # Add a blank character to the start to attach the image link
                await bot.send_photo(chat_id=chat_id, photo=img_url, caption=text_msg, reply_markup=kb, parse_mode=ParseMode.HTML)
            except:
                await bot.send_message(chat_id=chat_id, text=text_msg, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=chat_id, text=text_msg, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        
        db.save_tweet_content(username, title, translation, img_url, x_link)
        db.mark_sent(chat_id, tid)
        logger.info(f"🚀 Sent @{username}")
    except Exception as e: logger.error(f"Error: {e}")

async def process_user(username, last_id, sem, bot):
    entries = await fetch_feed(username, sem)
    if not entries: return
    new_last_id = last_id
    for entry in reversed(entries[:3]):
        tid = extract_id(entry)
        if not tid or tid == last_id: continue
        for cid in db.get_subs_for_user(username): await process_single_tweet(cid, username, entry, bot)
        new_last_id = tid
    if new_last_id != last_id: db.update_last_id(username, new_last_id)

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    if not tracked: return
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    await asyncio.gather(*[process_user(u, li, sem, context.application.bot) for u, li in tracked])

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 Bot Active.")))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("test", cmd_test))
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
