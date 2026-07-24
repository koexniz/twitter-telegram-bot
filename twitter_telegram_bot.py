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

# Optimized RSS Sources
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
            # Fix Base URL for Requesty (Ensure /v1/chat/completions)
            base = REQUESTY_BASE_URL if "/v1" in REQUESTY_BASE_URL else f"{REQUESTY_BASE_URL}/v1"
            full_url = f"{base}/chat/completions"
            
            payload = {
                "model": REQUESTY_MODEL,
                "messages": [{"role": "user", "content": f"Translate to colloquial Persian. Keep crypto terms English: {text[:1000]}"}],
                "temperature": 0.2
            }
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.post(full_url, headers={"Authorization": f"Bearer {REQUESTY_API_KEY}"}, json=payload)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
                else:
                    logger.warning(f"AI Error: {resp.status_code}")
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
                async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
                    resp = await client.get(url)
                    # Filter out Ads and non-200 responses
                    if resp.status_code != 200 or "uni-sonia" in str(resp.url) or "google.com" in str(resp.url):
                        continue
                    feed = feedparser.parse(resp.text)
                    if feed.entries:
                        logger.info(f"✅ Success: @{username}")
                        return feed.entries
            except: continue
        return []

async def fetch_feed_task(username, semaphore):
    entries = await fetch_feed(username, semaphore)
    return username, entries

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Example: <code>/add user1 user2</code>", parse_mode=ParseMode.HTML)
        return
    raw_input = " ".join(context.args)
    all_usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    wait_msg = await update.message.reply_text(f"⏳ Processing {len(all_usernames)} accounts...")
    
    added, added_warn, skipped, failed = [], [], [], []
    batch_size = 10
    for i in range(0, len(all_usernames), batch_size):
        batch = all_usernames[i:i+batch_size]
        to_fetch = [u for u in batch if is_valid_twitter(u) and not db.is_subscribed(chat_id, u)]
        for u in batch:
            if not is_valid_twitter(u): failed.append(u)
            elif db.is_subscribed(chat_id, u): skipped.append(u)
        if to_fetch:
            sem = asyncio.Semaphore(5)
            results = await asyncio.gather(*[fetch_feed_task(u, sem) for u in to_fetch])
            for u, entries in results:
                last_id = extract_id(entries[0]) if entries else ""
                db.add_subscription(chat_id, u, last_id)
                if entries: added.append(f"@{u}")
                else: added_warn.append(f"@{u}")
    
    await wait_msg.delete()
    if added: await update.message.reply_text(f"🔹 Added: <code>{', '.join(added)}</code>", parse_mode=ParseMode.HTML)
    if added_warn: await update.message.reply_text(f"⚠️ Added (Feed Down): <code>{', '.join(added_warn)}</code>", parse_mode=ParseMode.HTML)
    if skipped: await update.message.reply_text(f"🔸 Already exist: <code>{', '.join(skipped)}</code>", parse_mode=ParseMode.HTML)
    if failed: await update.message.reply_text(f"❌ Invalid: <code>{', '.join(failed)}</code>", parse_mode=ParseMode.HTML)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Example: <code>/del user1 user2</code>", parse_mode=ParseMode.HTML)
        return
    raw_input = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    removed = [f"@{u}" for u in usernames if db.is_subscribed(chat_id, u)]
    for u in usernames: db.remove_subscription(chat_id, u)
    await update.message.reply_text(f"🗑 <b>Removed:</b> <code>{', '.join(removed) if removed else 'None'}</code>", parse_mode=ParseMode.HTML)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in db.get_all_tracked() if db.is_subscribed(chat_id, u)]
    msg = f"📋 <b>Your List ({len(my_users)}):</b>\n\n" + ("\n".join(my_users) if my_users else "Empty")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# --- Worker ---
async def process_user(username, last_id, sem, bot):
    entries = await fetch_feed(username, sem)
    if not entries:
        # logger.info(f"Empty feed for @{username}") # فعال کردن این خط لاگ را شلوغ میکند
        return
    
    new_last_id = last_id
    found_new = False
    
    for entry in reversed(entries[:3]):
        tid = extract_id(entry)
        
        # لاگ برای دیباگ:
        if not tid:
            continue
            
        if tid == last_id:
            # این توییت دقیقاً همان آخرین توییتی است که قبلاً خوانده شده
            continue

        translation = await translate_text(entry.get("title", ""))
        x_link = convert_to_x_link(entry.get('link', ''))
        cids = db.get_subs_for_user(username)
        
        for cid in cids:
            if not db.is_duplicate(cid, tid):
                try:
                    header = f"👤 <b>@{html.escape(username)}</b>"
                    body = f"<blockquote expandable>{html.escape(entry.get('title', '')[:1900])}</blockquote>"
                    text_msg = f"{header}\n{body}"
                    if translation:
                        text_msg += f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>Translate:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
                    
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=x_link)]])
                    
                    sent = await bot.send_message(chat_id=cid, text=text_msg, reply_markup=kb, parse_mode=ParseMode.HTML)
                    if sent:
                        logger.info(f"✈️ Message sent to {cid} for @{username} (Tweet ID: {tid})")
                        db.mark_sent(cid, tid)
                        found_new = True
                except Exception as e:
                    logger.error(f"❌ Telegram Error for {username}: {e}")
        
        new_last_id = tid

    if new_last_id != last_id:
        db.update_last_id(username, new_last_id)

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    if not tracked: return
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    await asyncio.gather(*[process_user(u, li, sem, context.application.bot) for u, li in tracked])

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 Bot is active.")))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
