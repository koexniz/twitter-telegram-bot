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

db = Database("data/bot_data.db")

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

async def fetch_feed(username, semaphore):
    async with semaphore:
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        feed = feedparser.parse(resp.text)
                        if feed.entries: return feed.entries
            except: continue
        return []

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ مثال: `/add user1 user2`", parse_mode=ParseMode.MARKDOWN)
        return
    
    raw_input = " ".join(context.args)
    usernames = [clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]
    chat_id = str(update.effective_chat.id)
    added, skipped, failed = [], [], []

    wait = await update.message.reply_text("⏳ در حال بررسی...")
    sem = asyncio.Semaphore(2)

    for u in usernames:
        if not is_valid_twitter(u):
            failed.append(u); continue
        if db.is_subscribed(chat_id, u):
            skipped.append(u); continue
        
        entries = await fetch_feed(u, sem)
        last_id = extract_id(entries[0]) if entries else ""
        db.add_subscription(chat_id, u, last_id)
        added.append(f"@{u}")

    res = "✅ **گزارش:**\n"
    if added: res += f"🔹 اضافه شد: {', '.join(added)}\n"
    if skipped: res += f"🔸 قبلاً بود: {', '.join(skipped)}\n"
    if failed: res += f"❌ نامعتبر: {', '.join(failed)}"
    await wait.edit_text(res, parse_mode=ParseMode.MARKDOWN)

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
