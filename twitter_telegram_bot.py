import os
import asyncio
import logging
import feedparser
import re
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from database import Database

load_dotenv()

# Config
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
CONCURRENT_LIMIT = 5

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()

RSS_SOURCES = [
    "https://rsshub.app/twitter/user/{username}",
    "https://nitter.net/{username}/rss",
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

async def fetch_feed_task(username, semaphore):
    """تابع کمکی برای اجرای موازی"""
    entries = await fetch_feed(username, semaphore)
    return username, entries

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ مثال: `/add user1 user2`")
        return
    
    raw_input = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    
    wait = await update.message.reply_text(f"⏳ در حال استعلام موازی {len(usernames)} اکانت...")
    
    # فیلتر کردن یوزرهای نامعتبر یا تکراری قبل از استعلام
    to_fetch = []
    skipped = []
    failed = []
    for u in usernames:
        if not is_valid_twitter(u):
            failed.append(u)
        elif db.is_subscribed(chat_id, u):
            skipped.append(u)
        else:
            to_fetch.append(u)

    # اجرای تمام استعلام‌ها به صورت همزمان
    semaphore = asyncio.Semaphore(10) # اجازه ۱۰ درخواست همزمان
    tasks = [fetch_feed_task(u, semaphore) for u in to_fetch]
    
    results = await asyncio.gather(*tasks)
    
    added = []
    for username, entries in results:
        if entries:
            last_id = extract_id(entries[0])
            db.add_subscription(chat_id, username, last_id)
            added.append(f"@{username}")
        else:
            failed.append(username)

    # ساخت گزارش نهایی
    res_parts = ["✅ **گزارش نهایی:**"]
    if added: res_parts.append(f"🔹 اضافه شد ({len(added)}): {', '.join(added)}")
    if skipped: res_parts.append(f"🔸 از قبل بود ({len(skipped)}): {', '.join(skipped)}")
    if failed: res_parts.append(f"❌ ناموفق ({len(failed)}): {', '.join(failed)}")
    
    final_text = "\n\n".join(res_parts)
    if len(final_text) > 4000: final_text = final_text[:3900] + "..."
    
    await wait.edit_text(final_text, parse_mode=ParseMode.MARKDOWN)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    all_tracked = db.get_all_tracked()
    my_users = []
    for u, _ in all_tracked:
        if db.is_subscribed(chat_id, u):
            my_users.append(f"@{u}")
    
    if not my_users:
        await update.message.reply_text("لیست شما خالی است.")
    else:
        await update.message.reply_text("📋 **لیست شما:**\n" + "\n".join(my_users), parse_mode=ParseMode.MARKDOWN)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    username = clean_username(context.args[0])
    db.remove_subscription(update.effective_chat.id, username)
    await update.message.reply_text(f"🗑 @{username} حذف شد.")

# --- Background Task ---
async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    if not tracked: return
    
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    tasks = []
    for username, last_id in tracked:
        tasks.append(process_user(username, last_id, sem, context.application.bot))
    await asyncio.gather(*tasks)

async def process_user(username, last_id, sem, bot):
    entries = await fetch_feed(username, sem)
    if not entries: return
    
    new_last_id = last_id
    for entry in reversed(entries[:5]):
        tid = extract_id(entry)
        if not tid or tid == last_id: continue
        
        for cid in db.get_subs_for_user(username):
            if not db.is_duplicate(cid, tid):
                try:
                    text = f"🐦 **@{username}**\n\n{entry.get('title')}"
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("View", url=entry.get('link'))]])
                    await bot.send_message(chat_id=cid, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
                    db.mark_sent(cid, tid)
                except: pass
        new_last_id = tid
    
    if new_last_id != last_id:
        db.update_last_id(username, new_last_id)

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("خوش آمدید!")))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    logger.info("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
