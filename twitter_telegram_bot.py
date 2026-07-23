import os
import asyncio
import logging
import feedparser
import re
import httpx
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from database import Database  # فرض بر اینکه فایل بالا را ذخیره کردید

load_dotenv()
db = Database("data/bot_data.db") # حالا این متد خودش پوشه را می‌سازد

# Config
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_CHAT_ID", "").split(",") if x.strip()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))
CONCURRENT_LIMIT = 10  # تعداد اکانت‌هایی که همزمان چک می‌شوند

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database("data/bot_data.db") # در ریلیوی پوشه data را مپ کنید

RSS_SOURCES = [
    "https://rsshub.app/twitter/user/{username}",
    "https://nitter.net/{username}/rss",
    "https://xcancel.com/{username}/rss"
]

async def fetch_feed(username, semaphore):
    async with semaphore:
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(url)
                    if response.status_code == 200:
                        feed = feedparser.parse(response.text)
                        if feed.entries:
                            return feed.entries
            except Exception as e:
                logger.debug(f"Source failed for {username}: {url} -> {e}")
        return []

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    
    tasks = []
    for username, last_id in tracked:
        tasks.append(process_user(username, last_id, semaphore, context.application.bot))
    
    await asyncio.gather(*tasks)

async def process_user(username, last_id, semaphore, bot):
    entries = await fetch_feed(username, semaphore)
    if not entries:
        return

    new_last_id = last_id
    # برعکس کردن لیست برای ارسال از قدیمی به جدید
    for entry in reversed(entries[:5]): 
        tweet_id = extract_id(entry)
        if not tweet_id or tweet_id == last_id:
            continue
        
        chats = db.get_subs_for_user(username)
        for chat_id in chats:
            if not db.is_duplicate(chat_id, tweet_id):
                await send_tweet(chat_id, username, entry, bot)
                db.mark_sent(chat_id, tweet_id)
        
        new_last_id = tweet_id

    if new_last_id != last_id:
        db.update_last_id(username, new_last_id)

def extract_id(entry):
    link = entry.get("link", "")
    m = re.search(r"status/(\d+)", link)
    return m.group(1) if m else None

async def send_tweet(chat_id, username, entry, bot):
    title = entry.get("title", "New Tweet")
    link = entry.get("link", "")
    
    # ساده‌سازی برای مثال - اینجا می‌توانید منطق ترجمه را اضافه کنید
    text = f"🐦 **@{username}**\n\n{title}"
    
    keyboard = [[InlineKeyboardButton("مشاهده در توییتر", url=link)]]
    try:
        await bot.send_message(
            chat_id=chat_id, 
            text=text, 
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error sending to {chat_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ربات مانیتورینگ توییتر فعال است.\nاز دستور /add استفاده کنید.")

def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found!")
        return

    # ساخت پوشه دیتا برای ریلیوی اگر وجود ندارد
    os.makedirs("data", exist_ok=True)

    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    # سایر هندلرها را اینجا اضافه کنید (Add, Del, List)

    job_queue = app.job_queue
    job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
