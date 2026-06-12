import os
import json
import asyncio
import logging
import feedparser
import html
import re
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

DATA_FILE = "tracked_users.json"
FILTERS_FILE = "filters.json"
CHECK_INTERVAL = 90
TRANSLATE_FA = os.getenv("TRANSLATE_FA", "true").lower() == "true"
RSS_HUB_URL = os.getenv("RSS_HUB_URL", "https://rsshub.app").rstrip("/")

# RSS sources - RSS_HUB اول چک میشه که پایدارتره
RSS_SOURCES = [
    RSS_HUB_URL + "/twitter/user/{username}",
    "https://rsshub.rssforever.com/twitter/user/{username}",
    "https://xcancel.com/{username}/rss",
    "https://nitter.poast.org/{username}/rss",
    "https://nitter.net/{username}/rss",
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Translation ---
translator = None
if TRANSLATE_FA:
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source='auto', target='fa')
        logger.info("Translator FA enabled")
    except Exception as e:
        logger.warning(f"Translator disabled: {e}")
        translator = None

_translate_cache = {}
def translate_fa(text: str) -> str | None:
    if not translator or not text.strip():
        return None
    if re.search(r'[\u0600-\u06FF]', text):
        return None
    if text in _translate_cache:
        return _translate_cache[text]
    try:
        result = translator.translate(text[:4500])
        _translate_cache[text] = result
        return result
    except Exception as e:
        logger.warning(f"Translate failed: {e}")
        return None

# --- Storage ---
def load_json(path, default):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

tracked = load_json(DATA_FILE, {})
filters_db = load_json(FILTERS_FILE, {"global": {"filter_rt": True, "filter_replies": True}, "chats": {}})

def save_filters():
    save_json(FILTERS_FILE, filters_db)

def get_chat_filters(chat_id):
    chat_id = str(chat_id)
    if chat_id not in filters_db["chats"]:
        filters_db["chats"][chat_id] = {"keywords": [], "alert_keywords": []}
    # مهاجرت از نسخه قدیمی
    cf = filters_db["chats"][chat_id]
    cf.setdefault("keywords", [])
    cf.setdefault("alert_keywords", [])
    return cf

def clean_username(raw: str) -> str:
    raw = raw.strip().lstrip('@')
    if 'x.com/' in raw:
        raw = raw.split('x.com/')[-1]
    if 'twitter.com/' in raw:
        raw = raw.split('twitter.com/')[-1]
    raw = raw.split('?')[0].split('/')[0]
    return raw.lower()

def get_rss_feed(username):
    username = clean_username(username)
    for template in RSS_SOURCES:
        url = template.format(username=username)
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
            if not feed.entries:
                continue
            first_title = (feed.entries[0].get('title', '') or '').lower()
            if 'whitelist' in first_title or 'rss reader' in first_title or 'not yet' in first_title:
                continue
            return feed
        except Exception as e:
            logger.warning(f"Failed {url}: {e}")
            continue
    return None

def extract_tweet_id(entry):
    link = entry.get('link', '')
    if '/status/' in link:
        try:
            return link.split('/status/')[1].split('#')[0].split('?')[0]
        except: pass
    return entry.get('id', link)

def is_retweet(text: str) -> bool:
    t = text.strip()
    return t.startswith('RT @') or t.startswith('RT ')

def is_reply(text: str, username: str) -> bool:
    t = text.strip()
    # ریپلای معمولا با @ شروع میشه و یوزر خود طرف نیست
    if t.startswith('@'):
        first_word = t.split()[0].lower().lstrip('@')
        return first_word != username.lower()
    return False

def should_send(chat_id, username, text):
    g = filters_db["global"]
    cf = get_chat_filters(chat_id)
    low = text.lower()

    # 1. آلارم طلایی - همه فیلترها رو bypass میکنه
    alert_keywords = cf.get("alert_keywords", [])
    is_alert = any(k.lower() in low for k in alert_keywords) if alert_keywords else False
    if is_alert:
        return True, "alert", True

    # 2. فیلتر RT
    if g.get("filter_rt", True) and is_retweet(text):
        return False, "retweet", False
    # 3. فیلتر ریپلای
    if g.get("filter_replies", True) and is_reply(text, username):
        return False, "reply", False
    # 4. فیلتر کلمات کلیدی معمولی
    keywords = cf.get("keywords", [])
    if keywords:
        if not any(k.lower() in low for k in keywords):
            return False, "keyword", False
    return True, "", False

async def send_tweet_entry(chat_id, username, entry, bot):
    title = html.unescape(entry.get('title', ''))
    # حذف "username: " اول توییت که Nitter اضافه میکنه
    if re.match(rf'^{re.escape(username)}\s*:\s*', title, re.IGNORECASE):
        title = re.sub(rf'^{re.escape(username)}\s*:\s*', '', title, flags=re.IGNORECASE)

    # فیلتر + آلارم
    ok, reason, is_alert = should_send(chat_id, username, title)
    if not ok:
        return False, reason

    link = entry.get('link', '')
    for inst in ['nitter.poast.org', 'nitter.net', 'xcancel.com', 'nitter.']:
        link = link.replace(inst, 'x.com')
    link = link.replace('twitter.com', 'x.com')

    tweet_id = extract_tweet_id(entry)

    fa_text = translate_fa(title) if TRANSLATE_FA and translator else None
    
    # متن پیام
    if is_alert:
        text = f"🚨🚨 <b>ALERT</b> 🚨🚨\n🐦 @{username}\n\n{html.escape(title)}"
    else:
        text = f"🐦 <b>@{username}</b>\n\n{html.escape(title)}"
    
    if fa_text and fa_text.strip() != title.strip():
        text += f"\n\n━━━━━━━\n🇮🇷 <b>ترجمه:</b>\n{html.escape(fa_text)}"

    # دکمه‌های شیشه‌ای
    keyboard = [[
        InlineKeyboardButton("🔗 مشاهده در X", url=link),
    ],[
        InlineKeyboardButton("❤️ Like", url=f"https://x.com/intent/like?tweet_id={tweet_id}"),
        InlineKeyboardButton("🔁 RT", url=f"https://x.com/intent/retweet?tweet_id={tweet_id}"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    image_url = None
    if 'enclosures' in entry and entry.enclosures:
        image_url = entry.enclosures[0].get('href')
    if not image_url and 'description' in entry:
        m = re.search(r'<img src="([^"]+)"', entry.description)
        if m:
            image_url = m.group(1)

    # توییت عادی سایلنت، آلارم با صدا
    disable_notification = not is_alert

    sent_msg = None
    try:
        if image_url and image_url.startswith('http'):
            sent_msg = await bot.send_photo(chat_id=chat_id, photo=image_url, caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_notification=disable_notification)
        else:
            sent_msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False, reply_markup=reply_markup, disable_notification=disable_notification)
    except Exception as e:
        logger.error(f"Send failed: {e}")
        # اگر کپشن طولانی بود بدون عکس
        try:
            sent_msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_notification=disable_notification)
        except Exception as e2:
            logger.error(f"Send failed 2: {e2}")
            return False, "error"

    # پین کردن آلارم‌ها
    if is_alert and sent_msg:
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
        except Exception:
            pass  # توی PV پین نمیشه، مهم نیست

    return True, "alert" if is_alert else "sent"

async def check_twitter_updates(app: Application):
    while True:
        if not tracked:
            await asyncio.sleep(CHECK_INTERVAL)
            continue
        for username, info in list(tracked.items()):
            try:
                feed = get_rss_feed(username)
                if not feed or not feed.entries:
                    continue
                last_id = info.get("last_id")
                new_entries = []
                for entry in feed.entries:
                    tweet_id = extract_tweet_id(entry)
                    if tweet_id == last_id:
                        break
                    new_entries.append(entry)
                for entry in reversed(new_entries):
                    tweet_id = extract_tweet_id(entry)
                    for chat_id in info.get("chats", []):
                        await send_tweet_entry(chat_id, username, entry, app.bot)
                        await asyncio.sleep(0.3)
                    tracked[username]["last_id"] = tweet_id
                    save_json(DATA_FILE, tracked)
                await asyncio.sleep(1.5)
            except Exception as e:
                logger.error(f"Check failed for {username}: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tr_status = "فعال ✅" if translator else "غیرفعال ❌"
    g = filters_db["global"]
    cf = get_chat_filters(update.effective_chat.id)
    alert_count = len(cf.get("alert_keywords", []))
    await update.message.reply_text(
        f"🤖 Twitter Follower v4.1\nترجمه: {tr_status}\nفیلتر RT: {'✅' if g['filter_rt'] else '❌'} | ریپلای: {'✅' if g['filter_replies'] else '❌'}\nآلارم فعال: {alert_count} کلمه\n\n"
        "➕ اضافه کردن (تکی/دسته‌جمعی):\n"
        "/add vitalikbuterin\n"
        "/add user1 user2 user3\n"
        "یا لیست بلند با کاما/خط جدید\n\n"
        "/remove <user> - حذف\n"
        "/list - لیست\n"
        "/check [@user] [n] - چک دستی\n"
        "/translate - ترجمه on/off\n"
        "/filter rt on/off\n"
        "/keywords add a b c\n"
        "/alert add airdrop listing\n"
    )

async def toggle_translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global translator
    global TRANSLATE_FA
    TRANSLATE_FA = not TRANSLATE_FA
    if TRANSLATE_FA and translator is None:
        try:
            from deep_translator import GoogleTranslator
            translator = GoogleTranslator(source='auto', target='fa')
        except:
            await update.message.reply_text("deep_translator نصب نیست: pip install deep-translator")
            TRANSLATE_FA = False
            return
    await update.message.reply_text(f"ترجمه {'فعال ✅' if TRANSLATE_FA else 'خاموش ❌'}")

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    g = filters_db["global"]
    if not context.args:
        await update.message.reply_text(
            f"وضعیت فعلی:\nRT filter: {'ON' if g['filter_rt'] else 'OFF'}\nReplies filter: {'ON' if g['filter_replies'] else 'OFF'}\n\n"
            "استفاده:\n/filter rt on\n/filter rt off\n/filter replies on\n/filter replies off\n/filter status"
        )
        return
    if context.args[0] == "status":
        await update.message.reply_text(f"RT: {'ON' if g['filter_rt'] else 'OFF'}\nReplies: {'ON' if g['filter_replies'] else 'OFF'}")
        return
    if len(context.args) >= 2:
        what, val = context.args[0].lower(), context.args[1].lower()
        on = val in ("on", "1", "true", "yes")
        if what in ("rt", "retweet", "retweets"):
            g["filter_rt"] = on
            save_filters()
            await update.message.reply_text(f"فیلتر ریتوییت {'فعال' if on else 'خاموش'} شد")
            return
        if what in ("replies", "reply"):
            g["filter_replies"] = on
            save_filters()
            await update.message.reply_text(f"فیلتر ریپلای {'فعال' if on else 'خاموش'} شد")
            return
    await update.message.reply_text("دستور اشتباه. /filter rt on")

async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cf = get_chat_filters(chat_id)
    if not context.args:
        kws = cf.get("keywords", [])
        await update.message.reply_text(
            "کلمات کلیدی فعلی: " + (", ".join(kws) if kws else "هیچی - همه توییت‌ها ارسال میشه") +
            "\n\n/keywords add airdrop listing\n/keywords clear\n/keywords list"
        )
        return
    cmd = context.args[0].lower()
    if cmd == "clear":
        cf["keywords"] = []
        save_filters()
        await update.message.reply_text("✅ کلمات کلیدی پاک شد. همه توییت‌ها ارسال میشه.")
        return
    if cmd == "list":
        kws = cf.get("keywords", [])
        await update.message.reply_text("کلمات: " + (", ".join(kws) if kws else "هیچی"))
        return
    if cmd == "add":
        new_kws = [k.lower() for k in context.args[1:] if k]
        if not new_kws:
            await update.message.reply_text("مثال: /keywords add airdrop listing mainnet")
            return
        kws = cf.get("keywords", [])
        for k in new_kws:
            if k not in kws:
                kws.append(k)
        cf["keywords"] = kws
        save_filters()
        await update.message.reply_text("✅ اضافه شد. الان فیلتر: " + ", ".join(kws))
        return
    # اگر بدون add نوشت، همون‌ها رو اضافه کن
    new_kws = [k.lower() for k in context.args if k]
    kws = cf.get("keywords", [])
    for k in new_kws:
        if k not in kws:
            kws.append(k)
    cf["keywords"] = kws
    save_filters()
    await update.message.reply_text("✅ کلمات کلیدی: " + ", ".join(kws))

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """آلارم طلایی - توییت حاوی این کلمات با صدا + پین میاد"""
    chat_id = update.effective_chat.id
    cf = get_chat_filters(chat_id)
    if not context.args:
        kws = cf.get("alert_keywords", [])
        await update.message.reply_text(
            "🚨 کلمات آلارم فعلی: " + (", ".join(kws) if kws else "هیچی") +
            "\n\nتوییت عادی سایلنت میاد، ولی اگر کلمه آلارم توش باشه با صدا + پین میشه.\n\n"
            "/alert add airdrop listing snapshot hack\n"
            "/alert clear\n"
            "/alert list"
        )
        return
    cmd = context.args[0].lower()
    if cmd == "clear":
        cf["alert_keywords"] = []
        save_filters()
        await update.message.reply_text("✅ آلارم‌ها پاک شد.")
        return
    if cmd == "list":
        kws = cf.get("alert_keywords", [])
        await update.message.reply_text("آلارم‌ها: " + (", ".join(kws) if kws else "هیچی"))
        return
    if cmd == "add":
        new_kws = [k.lower() for k in context.args[1:] if k]
        if not new_kws:
            await update.message.reply_text("مثال: /alert add airdrop listing snapshot")
            return
        kws = cf.get("alert_keywords", [])
    else:
        # بدون add
        new_kws = [k.lower() for k in context.args if k]
        kws = cf.get("alert_keywords", [])
    
    for k in new_kws:
        if k not in kws:
            kws.append(k)
    cf["alert_keywords"] = kws
    save_filters()
    await update.message.reply_text("🚨 آلارم فعال برای: " + ", ".join(kws) + "\nتوییت‌های عادی سایلنت، آلارم با صدا + پین")

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # استخراج لیست یوزرنیم‌ها - ساپورت فاصله، کاما، خط جدید
    raw_text = ""
    if update.message and update.message.text:
        parts = update.message.text.split(None, 1)
        if len(parts) > 1:
            raw_text = parts[1]
    if not raw_text and context.args:
        raw_text = " ".join(context.args)
    
    if not raw_text.strip():
        await update.message.reply_text(
            "استفاده:\n"
            "/add vitalikbuterin\n"
            "/add user1 user2 user3\n"
            "یا لیست بلند:\n"
            "/add user1, user2, user3\n"
            "یا هر کدوم تو یه خط"
        )
        return

    # پارس کردن: کاما، فاصله، خط جدید
    raw_usernames = re.split(r'[,\s\n\r\t]+', raw_text)
    usernames = []
    seen = set()
    for u in raw_usernames:
        u = clean_username(u)
        if u and u not in seen and re.match(r'^[a-z0-9_]{1,15}$', u):
            seen.add(u)
            usernames.append(u)
    
    if not usernames:
        await update.message.reply_text("هیچ یوزرنیم معتبری پیدا نکردم.")
        return

    # اگر تکی بود، همون رفتار قبلی
    if len(usernames) == 1:
        username = usernames[0]
        msg = await update.message.reply_text(f"در حال بررسی @{username} ...")
        feed = get_rss_feed(username)
        if not feed or not feed.entries:
            await msg.edit_text(f"❌ @{username} پیدا نشد.")
            return
        last_id = extract_tweet_id(feed.entries[0])
        if username not in tracked:
            tracked[username] = {"last_id": last_id, "chats": []}
        if chat_id not in tracked[username]["chats"]:
            tracked[username]["chats"].append(chat_id)
        save_json(DATA_FILE, tracked)
        await msg.edit_text(f"✅ @{username} اضافه شد!")
        return

    # حالت دسته‌جمعی
    status_msg = await update.message.reply_text(f"📥 در حال اضافه کردن {len(usernames)} اکانت...\n0/{len(usernames)}")
    added, failed, existed = [], [], []
    
    for i, username in enumerate(usernames, 1):
        try:
            # اگر قبلا اضافه شده
            if username in tracked and chat_id in tracked[username].get("chats", []):
                existed.append(username)
            else:
                feed = get_rss_feed(username)
                if feed and feed.entries:
                    last_id = extract_tweet_id(feed.entries[0])
                    if username not in tracked:
                        tracked[username] = {"last_id": last_id, "chats": []}
                    if chat_id not in tracked[username]["chats"]:
                        tracked[username]["chats"].append(chat_id)
                    save_json(DATA_FILE, tracked)
                    added.append(username)
                else:
                    failed.append(username)
        except Exception:
            failed.append(username)
        
        # هر 5 تا آپدیت بده
        if i % 5 == 0 or i == len(usernames):
            try:
                await status_msg.edit_text(
                    f"📥 در حال اضافه کردن {len(usernames)} اکانت...\n"
                    f"{i}/{len(usernames)} | ✅ {len(added)} | ❌ {len(failed)}"
                )
            except: pass
        await asyncio.sleep(0.8)  # ضد ریت لیمیت

    # گزارش نهایی
    report = f"✅ تمام شد!\n\nاضافه شد: {len(added)}\nتکراری: {len(existed)}\nناموفق: {len(failed)}"
    if failed:
        report += "\n\nناموفق‌ها:\n" + ", ".join([f"@{u}" for u in failed[:10]])
        if len(failed) > 10:
            report += f" و {len(failed)-10} مورد دیگر"
    await status_msg.edit_text(report)

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    raw_text = ""
    if update.message and update.message.text:
        parts = update.message.text.split(None, 1)
        if len(parts) > 1:
            raw_text = parts[1]
    if not raw_text and context.args:
        raw_text = " ".join(context.args)
    
    if not raw_text.strip():
        await update.message.reply_text("استفاده: /remove username\nیا چندتایی: /remove user1 user2 user3")
        return

    raw_usernames = re.split(r'[,\s\n\r\t]+', raw_text)
    usernames = []
    seen = set()
    for u in raw_usernames:
        u = clean_username(u)
        if u and u not in seen:
            seen.add(u)
            usernames.append(u)

    removed = []
    not_found = []
    for username in usernames:
        if username in tracked and chat_id in tracked[username].get("chats", []):
            tracked[username]["chats"].remove(chat_id)
            if not tracked[username]["chats"]:
                del tracked[username]
            removed.append(username)
        else:
            not_found.append(username)
    
    save_json(DATA_FILE, tracked)
    msg = f"✅ حذف شد: {len(removed)}"
    if removed:
        msg += "\n" + ", ".join([f"@{u}" for u in removed])
    if not_found:
        msg += f"\n\nپیدا نشد: {len(not_found)}"
    await update.message.reply_text(msg)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    my_accounts = [u for u, info in tracked.items() if chat_id in info.get("chats", [])]
    if not my_accounts:
        await update.message.reply_text("هیچ اکانتی نداری.")
        return
    await update.message.reply_text("📋 اکانت‌ها:\n" + "\n".join([f"• @{u}" for u in my_accounts]))

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args.copy() if context.args else []
    count = 1
    if args and args[-1].isdigit():
        count = int(args.pop())
        count = max(1, min(count, 5))
    if args:
        usernames = [clean_username(a) for a in args]
    else:
        usernames = [u for u, info in tracked.items() if chat_id in info.get("chats", [])]
    if not usernames:
        await update.message.reply_text("هیچ اکانتی نداری. /add")
        return
    await update.message.reply_text(f"🔍 {len(usernames)} اکانت، {count} توییت...")
    sent = 0
    filtered = 0
    for username in usernames:
        feed = get_rss_feed(username)
        if not feed or not feed.entries:
            continue
        for entry in reversed(feed.entries[:count]):
            ok, reason = await send_tweet_entry(chat_id, username, entry, context.bot)
            if ok:
                sent += 1
            else:
                filtered += 1
            await asyncio.sleep(0.4)
    msg = f"✅ تمام شد. ارسال: {sent}"
    if filtered:
        msg += f" | فیلتر شده: {filtered}"
    await update.message.reply_text(msg)

from telegram import BotCommand

BOT_COMMANDS = [
    BotCommand("add", "➕ اضافه کردن اکانت توییتر"),
    BotCommand("remove", "➖ حذف اکانت"),
    BotCommand("list", "📋 لیست اکانت‌های دنبال شده"),
    BotCommand("check", "🔍 چک دستی توییت‌ها"),
    BotCommand("alert", "🚨 آلارم کلمات طلایی"),
    BotCommand("keywords", "🔑 فیلتر کلمات کلیدی"),
    BotCommand("filter", "⚙️ فیلتر ریتوییت/ریپلای"),
    BotCommand("translate", "🌐 ترجمه فارسی on/off"),
    BotCommand("start", "❓ راهنما"),
]

def main():
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN رو در فایل .env قرار بده")
        return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("check", check_now))
    app.add_handler(CommandHandler("translate", toggle_translate))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("alert", cmd_alert))

    async def post_init(application: Application):
        # ست کردن منوی دستورات تلگرام - که با / نشون داده بشه
        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
        except Exception as e:
            logger.warning(f"set_my_commands failed: {e}")
        asyncio.create_task(check_twitter_updates(application))
    app.post_init = post_init
    print(f"Bot running... translate={bool(translator)} rsshub={RSS_HUB_URL}")
    app.run_polling()

if __name__ == "__main__":
    main()
