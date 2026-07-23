import os
import re
import json
import logging
import asyncio
from typing import Optional, Any, Dict

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------
# تنظیمات لاگینگ
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# متغیرهای محیطی
# ---------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "cbf5caed10msh6eef77ac9dc816fp12095bjsnfd641f9fe9c0")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # بررسی هر ۵ دقیقه
TRACKED_FILE = "tracked_accounts.json"

# لیست اندپوینت‌های چرخشی RapidAPI
RAPID_ENDPOINTS = [
    {
        "host": "twitter32.p.rapidapi.com",
        "url": "https://twitter32.p.rapidapi.com/getUserByScreenName?screen_name={username}"
    },
    {
        "host": "twitter-v24.p.rapidapi.com",
        "url": "https://twitter-v24.p.rapidapi.com/user/about?username={username}"
    }
]

# دیتابیس حافظه
TRACKED_DATA: Dict[str, Dict[str, str]] = {}


# ---------------------------------------------------------
# توابع کمکی
# ---------------------------------------------------------
def clean_username(raw: str) -> str:
    u = raw.strip().replace("@", "")
    if "twitter.com/" in u or "x.com/" in u:
        u = u.split("/")[-1].split("?")[0]
    return u.lower()


def valid_username(u: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9_]{1,15}$", u))


def load_tracked():
    global TRACKED_DATA
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, "r", encoding="utf-8") as f:
                TRACKED_DATA = json.load(f)
        except Exception as e:
            logger.error(f"خطا در خواندن فایل ذخیره: {e}")
            TRACKED_DATA = {}


def save_tracked():
    try:
        with open(TRACKED_FILE, "w", encoding="utf-8") as f:
            json.dump(TRACKED_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"خطا در ذخیره‌سازی: {e}")


# ---------------------------------------------------------
# دریافت توئیت‌ها از API
# ---------------------------------------------------------
async def fetch_rss_feed(username: str) -> Optional[Any]:
    username = clean_username(username)
    if not valid_username(username) or not RAPIDAPI_KEY:
        return None

    async with httpx.AsyncClient(timeout=10.0) as client:
        for ep in RAPID_ENDPOINTS:
            headers = {
                "x-rapidapi-host": ep["host"],
                "x-rapidapi-key": RAPIDAPI_KEY,
                "Content-Type": "application/json",
            }
            url = ep["url"].format(username=username)

            try:
                res = await client.get(url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    entries = []

                    user_data = data.get("data", {}).get("user", {}).get("result", {}) or data
                    timeline = user_data.get("timeline", {}) or user_data.get("tweets", [])

                    if isinstance(timeline, dict):
                        instructions = timeline.get("instructions", [])
                        for item in instructions:
                            for entry in item.get("entries", []):
                                tweet_res = entry.get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {})
                                legacy = tweet_res.get("legacy", {})
                                tid = legacy.get("id_str") or tweet_res.get("rest_id")
                                text = legacy.get("full_text") or legacy.get("text", "")

                                if tid and text:
                                    entries.append({
                                        "id": str(tid),
                                        "link": f"https://x.com/{username}/status/{tid}",
                                        "title": text,
                                        "summary": text,
                                    })
                    elif isinstance(timeline, list):
                        for t in timeline:
                            tid = t.get("id_str") or t.get("id")
                            text = t.get("full_text") or t.get("text", "")
                            if tid and text:
                                entries.append({
                                    "id": str(tid),
                                    "link": f"https://x.com/{username}/status/{tid}",
                                    "title": text,
                                    "summary": text,
                                })

                    if entries:
                        class DummyFeed:
                            pass

                        f = DummyFeed()
                        f.entries = entries
                        return f
            except Exception:
                continue

    return None


# ---------------------------------------------------------
# چک کردن خودکار دوره‌ای (Job)
# ---------------------------------------------------------
async def check_tweets_job(context: ContextTypes.DEFAULT_TYPE):
    if not TRACKED_DATA:
        return

    for chat_id, accounts in list(TRACKED_DATA.items()):
        for username, last_id in list(accounts.items()):
            feed = await fetch_rss_feed(username)
            if feed and hasattr(feed, "entries") and feed.entries:
                latest = feed.entries[0]
                latest_id = str(latest.get("id", ""))

                # اگر توئیت جدیدی ثبت شده بود
                if latest_id and latest_id != last_id:
                    TRACKED_DATA[chat_id][username] = latest_id
                    save_tracked()
                    
                    msg = (
                        f"🔔 <b>توئیت جدید از @{username}</b>\n\n"
                        f"{latest.get('title', '')}\n\n"
                        f"🔗 <a href='{latest.get('link', '')}'>مشاهده در ایکس</a>"
                    )
                    try:
                        await context.bot.send_message(
                            chat_id=int(chat_id),
                            text=msg,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=False,
                        )
                    except Exception as e:
                        logger.error(f"خطا در ارسال پیام به {chat_id}: {e}")


# ---------------------------------------------------------
# دستورات تلگرام
# ---------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>ربات مانیتورینگ توییتر فعال است!</b>\n\n"
        "دستورات عمومی:\n"
        "▫️ `/add username` - افزودن اکانت جدید\n"
        "▫️ `/remove username` - حذف اکانت\n"
        "▫️ `/list` - مشاهده لیست اکانت‌های فعال\n"
        "▫️ `/help` - راهنمای ربات"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ لطفاً نام کاربری را وارد کنید.\nمثال: `/add elonmusk`", parse_mode=ParseMode.MARKDOWN)
        return

    chat_id = str(update.effective_chat.id)
    raw_text = " ".join(context.args)
    raw_tokens = re.split(r"[\s,\n]+", raw_text)

    valid_users = []
    for token in raw_tokens:
        u = clean_username(token)
        if u and valid_username(u) and u not in valid_users:
            valid_users.append(u)

    if not valid_users:
        await update.message.reply_text("❌ هیچ نام کاربری معتبری یافت نشد.")
        return

    status_msg = await update.message.reply_text(f"⏳ در حال اضافه کردن {len(valid_users)} اکانت...")

    if chat_id not in TRACKED_DATA:
        TRACKED_DATA[chat_id] = {}

    added = []
    for u in valid_users:
        if u not in TRACKED_DATA[chat_id]:
            feed = await fetch_rss_feed(u)
            last_id = str(feed.entries[0].get("id", "")) if feed and hasattr(feed, "entries") and feed.entries else ""
            TRACKED_DATA[chat_id][u] = last_id
            added.append(u)

    if added:
        save_tracked()
        await status_msg.edit_text(f"✅ اکانت‌های زیر به لیست اضافه شدند:\n" + "\n".join(f"• @{u}" for u in added))
    else:
        await status_msg.edit_text("ℹ️ این اکانت(ها) قبلاً اضافه شده بودند.")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ لطفاً نام کاربری مورد نظر را وارد کنید.\nمثال: `/remove elonmusk`", parse_mode=ParseMode.MARKDOWN)
        return

    chat_id = str(update.effective_chat.id)
    u = clean_username(context.args[0])

    if chat_id in TRACKED_DATA and u in TRACKED_DATA[chat_id]:
        del TRACKED_DATA[chat_id][u]
        save_tracked()
        await update.message.reply_text(f"✅ اکانت @{u} از لیست حذف شد.")
    else:
        await update.message.reply_text(f"❌ اکانت @{u} در لیست یافت نشد.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    accounts = TRACKED_DATA.get(chat_id, {})

    if not accounts:
        await update.message.reply_text("📋 لیست اکانت‌های تحت نظر شما خالی است.\nبا دستور `/add` اکانت اضافه کنید.")
        return

    text = "📋 <b>اکانت‌های تحت نظر شما:</b>\n\n" + "\n".join(f"• @{u}" for u in accounts.keys())
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------
# اجرای اصلی
# ---------------------------------------------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("خطا: TELEGRAM_BOT_TOKEN ست نشده است!")
        return

    load_tracked()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ثبت دستورات
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))

    # زمان‌بندی بررسی توئیت‌ها
    if app.job_queue:
        app.job_queue.run_repeating(check_tweets_job, interval=CHECK_INTERVAL, first=10)

    logger.info("ربات با موفقیت روشن شد...")
    app.run_polling()


if __name__ == "__main__":
    main()
