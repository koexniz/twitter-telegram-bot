import os, asyncio, logging, feedparser, re, httpx, html, random
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from database import Database
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
import uvicorn

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
app = FastAPI()

# Web Setup
base_path = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(base_path, "templates"))

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
    desc = entry.get('description', '') or entry.get('summary', '')
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.I)
    if img_match:
        url = img_match.group(1)
        if "/pic/media%2F" in url:
            media_id = url.split("%2F")[-1].split('?')[0]
            return f"https://pbs.twimg.com/media/{media_id}"
        return url
    if 'media_content' in entry: return entry.media_content[0].get('url')
    return None

async def translate_text(text: str) -> str:
    if not TRANSLATE_FA or not text or len(re.findall(r"[\u0600-\u06FF]", text)) / len(re.findall(r"[A-Za-z\u0600-\u06FF]", text)) > 0.5 if re.findall(r"[A-Za-z\u0600-\u06FF]", text) else 0 > 0.5: return ""
    if REQUESTY_API_KEY:
        try:
            base = REQUESTY_BASE_URL if "/v1" in REQUESTY_BASE_URL else f"{REQUESTY_BASE_URL}/v1"
            payload = {"model": REQUESTY_MODEL, "messages": [{"role": "user", "content": f"Translate to colloquial Persian. Keep crypto terms English: {text[:1000]}"}], "temperature": 0.2}
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
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
                    if valid:
                        logger.info(f"✅ Success: @{username}")
                        return valid
            except: continue
        return []

# --- Bot Handlers ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args)
    users = list(set([clean_username(u) for u in re.split(r"[,\s]+", raw) if u]))
    for u in users:
        if is_valid_twitter(u) and not db.is_subscribed(update.effective_chat.id, u):
            db.add_subscription(update.effective_chat.id, u, "")
    await update.message.reply_text(f"✅ Monitoring started for: {', '.join(users)}")

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = clean_username(context.args[0]) if context.args else "ElonMusk"
    wait = await update.message.reply_text(f"🧪 Testing @{username}...")
    entries = await fetch_feed(username, asyncio.Semaphore(1))
    if entries:
        await process_single_tweet(update.effective_chat.id, username, entries[0], context.application.bot, force=True)
        await wait.delete()
    else: await wait.edit_text("❌ Feed Error.")

# --- Background Worker ---
async def process_single_tweet(chat_id, username, entry, bot, force=False):
    tid = extract_id(entry)
    if not tid or (not force and db.is_duplicate(chat_id, tid)): return
    try:
        title = entry.get("title", "")
        translation = await translate_text(title)
        img_url = extract_image_url(entry)
        text = f"🔔 <b>NEW UPDATE | @{html.escape(username).upper()}</b>\n\n📝 <b>Original:</b>\n<blockquote expandable>{html.escape(title[:1900])}</blockquote>"
        if translation: text += f"\n⎯⎯⎯⎯⎯⎯⎯\n🇮🇷 <b>Translate:</b>\n<blockquote expandable><i>{html.escape(translation[:1900])}</i></blockquote>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 View on X", url=f"https://x.com/i/status/{tid}")]])
        if img_url:
            try: await bot.send_photo(chat_id=chat_id, photo=img_url, caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except: await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else: await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        db.save_tweet_content(username, title, translation, img_url, f"https://x.com/i/status/{tid}")
        db.mark_sent(chat_id, tid)
        logger.info(f"🚀 Sent @{username}")
    except Exception as e: logger.error(f"Send Error: {e}")

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    tracked = db.get_all_tracked()
    sem = asyncio.Semaphore(CONCURRENT_LIMIT)
    await asyncio.gather(*[process_user(u, li, sem, context.application.bot) for u, li in tracked])

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

# --- Web App Routes ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    tweets = db.get_latest_tweets(30)
    return templates.TemplateResponse(name="index.html", context={"request": request, "tweets": tweets})

@app.get("/manifest.json")
async def get_manifest(): return FileResponse(os.path.join(base_path, "manifest.json"))

@app.get("/sw.js")
async def get_sw(): return FileResponse(os.path.join(base_path, "sw.js"))

# --- Main Runner ---
async def run_bot():
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🤖 Bot Active.")))
    bot_app.add_handler(CommandHandler("add", cmd_add))
    bot_app.add_handler(CommandHandler("test", cmd_test))
    bot_app.add_handler(CommandHandler("list", lambda u,c: u.message.reply_text("\n".join([f"• @{x[0]}" for x in db.get_all_tracked()]))))
    
    bot_app.job_queue.run_repeating(check_updates, interval=CHECK_INTERVAL, first=10)
    
    async with bot_app:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        # Keep running until the web server stops
        while True: await asyncio.sleep(1000)

if __name__ == "__main__":
    # Start bot in background thread, web server in main thread
    from threading import Thread
    Thread(target=lambda: asyncio.run(run_bot())).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
