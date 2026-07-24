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

# Translation Config
AEROLINK_API_KEY = os.getenv("AEROLINK_API_KEY", "").strip()
AEROLINK_BASE_URL = os.getenv("AEROLINK_BASE_URL", "").rstrip("/")
AEROLINK_MODEL = os.getenv("AEROLINK_MODEL", "gpt-4o-mini").strip()
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() in ("1", "true", "yes")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

db = Database()

RSS_SOURCES = [
    "https://nitter.privacydev.net/{username}/rss",
    "https://nitter.net/{username}/rss",
    "https://nitter.no-logs.com/{username}/rss",
    "https://nitter.perennialte.ch/{username}/rss",
    "https://rsshub.app/twitter/user/{username}"
]

# --- Helpers ---
def clean_username(raw: str) -> str:
    raw = (raw or "").strip()
    raw = raw.replace("https://", "").replace("http://", "")
    for domain in ["x.com/", "twitter.com/", "nitter.net/", "xcancel.com/"]:
        raw = raw.replace(domain, "")
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
    rss_domains = ["nitter.net", "nitter.privacydev.net", "nitter.poast.org", "nitter.no-logs.com", "nitter.perennialte.ch", "xcancel.com", "twitter.com"]
    for domain in rss_domains:
        if domain in link:
            return link.replace(domain, "x.com")
    if "rsshub.app" in link:
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
    if AEROLINK_API_KEY and AEROLINK_BASE_URL:
        try:
            full_url = f"{AEROLINK_BASE_URL}/chat/completions"
            prompt = f"Translate this tweet to colloquial Persian (Tehran dialect). Keep crypto terms (Airdrop, Mainnet, etc.) in English.\n\nText: {text}"
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.post(
                    full_url,
                    headers={"Authorization": f"Bearer {AEROLINK_API_KEY}"},
                    json={"model": AEROLINK_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
                )
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"].strip()
        except: pass
    try:
        from deep_translator import GoogleTranslator
        return await asyncio.to_thread(GoogleTranslator(source='auto', target='fa').translate, text)
    except: return ""

async def fetch_feed(username, semaphore):
    async with semaphore:
        await asyncio.sleep(random.uniform(1, 2))
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        for src in RSS_SOURCES:
            url = src.format(username=username)
            try:
                async with httpx.AsyncClient(timeout=10, headers=headers) as client:
                    resp = await client.get(url, follow_redirects=True)
                    if resp.status_code == 200:
                        feed = feedparser.parse(resp.text)
                        if feed.entries: return feed.entries
            except: continue
        return []

async def fetch_feed_task(username, semaphore):
    entries = await fetch_feed(username, semaphore)
    return username, entries

# --- Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ مثال: <code>/add user1 user2</code>", parse_mode=ParseMode.HTML)
        return
    raw_input = " ".join(context.args)
    all_usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    wait_msg = await update.message.reply_text(f"⏳ در حال پردازش {len(all_usernames)} اکانت...")
    
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
    res = "✅ <b>گزارش:</b>\n\n"
    if added: await update.message.reply_text(f"🔹 اضافه شد: <code>{', '.join(added)}</code>", parse_mode=ParseMode.HTML)
    if added_warn: await update.message.reply_text(f"⚠️ اضافه شد (فید قطع): <code>{', '.join(added_warn)}</code>", parse_mode=ParseMode.HTML)
    if skipped: await update.message.reply_text(f"🔸 قبلاً بود: <code>{', '.join(skipped)}</code>", parse_mode=ParseMode.HTML)
    if failed: await update.message.reply_text(f"❌ نامعتبر: <code>{', '.join(failed)}</code>", parse_mode=ParseMode.HTML)

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ مثال: <code>/del user1 user2</code>", parse_mode=ParseMode.HTML)
        return
    raw_input = " ".join(context.args)
    usernames = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw_input) if u]))
    chat_id = str(update.effective_chat.id)
    removed = []
    for u in usernames:
        if db.is_subscribed(chat_id, u):
            db.remove_subscription(chat_id, u)
            removed.append(f"@{u}")
    await update.message.reply_text(f"🗑 <b>حذف شد:</b> <code>{', '.join(removed) if removed else 'موردی یافت نشد'}</code>", parse_mode=ParseMode.HTML)

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    my_users = [f"• <code>{html.escape(u)}</code>" for u, _ in db.get_all_tracked() if db.is_subscribed(chat_id, u)]
    msg = f"📋 <b>لیست شما ({len(my_users)}):</b>\n\n" + ("\n".join(my_users) if my_users else "خالی")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# --- Background Worker ---
async def process_user(username, last_id, sem, bot):
    entries = await fetch_feed(username, sem)
    if not entries: return
    new_last_id = last_id
    for entry in reversed(entries[:3]):
        tid = extract_id(entry)
        if not tid or tid == last_id: continue
        title = entry.get("title", "")
        translation = await translate_text(title)
        x_link = convert_to_x_link(entry.get('link', ''))
        cids = db.get_subs_for_user(username)
        for cid in cids:
            if not db.is_duplicate(cid, tid):
                try:
                    header = f"👤 <b>@{html.escape(username)}</b>"
                    body = f"<blockquote>{html.escape(title)}</blockquote>" if len(title) > 100 else f"\n<b>{html.escape(title)}</b>"
                    text = f"{header}\n{body}"
                    if translation:
                        text += f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>ترجمه:</b>\n<blockquote><i>{html.escape(translation)}</i></blockquote>"
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 مشاهده در X", url=x_link)]])
                    await bot.send_message(chat_id=cid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                    db.mark_sent(cid, tid)
                except: pass
        new_last_id = tid
    if new_last_id != last_id: db.update_last_id(username, new_last_id)

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    if not tracked: return
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    await asyncio.gather(*[process_user(u, li, sem, context.application.bot) for u, li in tracked])

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 ربات فعال شد.")))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("list", cmd_list))
    app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
