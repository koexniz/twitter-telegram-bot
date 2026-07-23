import os
import asyncio
import logging
import feedparser
import re
import httpx
import html
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from database import Database

load_dotenv()

# --- Config ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
CONCURRENT_LIMIT = 10 # تعداد اکانت‌هایی که همزمان در پس‌زمینه چک می‌شوند

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()

RSS_SOURCES = [
    "https://nitter.privacydev.net/{username}/rss",
    "https://nitter.poast.org/{username}/rss",
    "https://nitter.moomoo.me/{username}/rss",
    "https://nitter.no-logs.com/{username}/rss",
    "https://nitter.projectsegfau.lt/{username}/rss",
    "https://xcancel.com/{username}/rss"
]

# --- Helpers ---
def clean_username(raw: str) -> str:
    raw = (raw or "").strip()
    raw = raw.replace("https://", "").replace("http://", "")
    raw = raw.replace("x.com/", "").replace("twitter.com/", "").lstrip("@")
    return raw.split("?")[0].split("/")[0].lower().strip()

def is_valid_twitter(username: str) -> bool:
    return bool(re.match(r"^[a-z0-9_]{1,15}$", username))

def extract_id(entry):
    link = entry.get("link", "")
    m = re.search(r"status/(\d+)", link)
    if m: return m.group(1)
    guid = entry.get("id", "")
    m = re.search(r"(\d{15,})", guid)
    return m.group(1) if m else None

async def fetch_feed(username, semaphore):
    """تابع اصلی واکشی RSS از سورس‌های مختلف"""
    async with semaphore:
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=7) as client:
                    resp = await client.get(url, follow_redirects=True)
                    if resp.status_code == 200:
                        feed = feedparser.parse(resp.text)
                        if feed.entries: return feed.entries
            except Exception:
                continue
        return []

async def fetch_feed_task(username, semaphore):
    entries = await fetch_feed(username, semaphore)
    return username, entries

# --- Handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 ربات مانیتورینگ توییتر فعال است!\n\n🔹 برای افزودن: `/add user1 user2` \n🔹 برای لیست: `/list` \n🔹 برای حذف: `/del user1`", parse_mode=ParseMode.HTML)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ مثال: <code>/add elonmusk</code>", parse_mode=ParseMode.HTML)
        return
    
    raw_input = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    
    wait = await update.message.reply_text(f"⏳ در حال بررسی {len(usernames)} اکانت...")
    
    to_fetch, skipped, failed = [], [], []
    for u in usernames:
        if not is_valid_twitter(u): failed.append(u)
        elif db.is_subscribed(chat_id, u): skipped.append(u)
        else: to_fetch.append(u)

    sem = asyncio.Semaphore(10)
    tasks = [fetch_feed_task(u, sem) for u in to_fetch]
    results = await asyncio.gather(*tasks)
    
    added = []
    added_with_warning = [] # اکانت‌هایی که اد شدند ولی سورس‌شان قطع بود

    for username, entries in results:
        if entries:
            last_id = extract_id(entries[0])
            db.add_subscription(chat_id, username, last_id)
            added.append(f"@{username}")
        else:
            # اگر سورس قطع بود، باز هم اد کن (Force Add)
            db.add_subscription(chat_id, username, "")
            added_with_warning.append(f"@{username}")

    res = "✅ <b>گزارش نهایی:</b>\n\n"
    if added: res += f"🔹 با موفقیت اضافه شد: <code>{', '.join(added)}</code>\n"
    if added_with_warning: res += f"⚠️ اضافه شد (سورس قطع است): <code>{', '.join(added_with_warning)}</code>\n"
    if skipped: res += f"🔸 قبلاً بود: <code>{', '.join(skipped)}</code>\n"
    if failed: res += f"❌ نامعتبر: <code>{', '.join(failed)}</code>"
    
    await wait.edit_text(res, parse_mode=ParseMode.HTML)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    all_tracked = db.get_all_tracked()
    my_users = [f"@{html.escape(u)}" for u, _ in all_tracked if db.is_subscribed(chat_id, u)]
    
    if not my_users:
        await update.message.reply_text("لیست شما خالی است.")
    else:
        await update.message.reply_text("📋 <b>لیست اکانت‌های شما:</b>\n\n" + "\n".join(my_users), parse_mode=ParseMode.HTML)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    username = clean_username(context.args[0])
    db.remove_subscription(update.effective_chat.id, username)
    await update.message.reply_text(f"🗑 <b>@{html.escape(username)}</b> از لیست حذف شد.", parse_mode=ParseMode.HTML)

# --- Background Worker ---
async def process_user(username, last_id, sem, bot):
    logger.info(f"🔍 Checking updates for @{username}...") # این خط را اضافه کن
    entries = await fetch_feed(username, sem)
    if not entries: return
    
    new_last_id = last_id
    for entry in reversed(entries[:5]):
        tid = extract_id(entry)
        if not tid or tid == last_id: continue
        
        cids = db.get_subs_for_user(username)
        for cid in cids:
            if not db.is_duplicate(cid, tid):
                try:
                    safe_name = html.escape(username)
                    safe_title = html.escape(entry.get("title", ""))
                    text = f"🐦 <b>@{safe_name}</b>\n\n{safe_title}"
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("مشاهده در X", url=entry.get('link'))]])
                    await bot.send_message(chat_id=cid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                    db.mark_sent(cid, tid)
                except Exception as e:
                    logger.error(f"Send error: {e}")
        new_last_id = tid
    
    if new_last_id != last_id:
        db.update_last_id(username, new_last_id)

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    if not tracked: return
    
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    tasks = [process_user(u, li, sem, context.application.bot) for u, li in tracked]
    await asyncio.gather(*tasks)

def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN missing!")
        return
        
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    
    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
