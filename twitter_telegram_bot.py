# ============================================================
#  نسخه اصلاح‌شده‌ی تابع cmd_add  (دستور /add)
#  جایگزین کنید با تابع cmd_add فعلی در فایل twitter_telegram_bot.py
# ============================================================
#
# مشکل نسخه اصلی:
#   فقط context.args[0] را می‌خواند یعنی فقط اولین اکانت اضافه می‌شود.
#   مثلاً  /add a b c  فقط a را اضافه می‌کند و b , c نادیده گرفته می‌شوند.
#
# راه‌حل:
#   روی همه‌ی آرگومان‌ها حلقه می‌زنیم و هر اکانت را جداگانه اعتبارسنجی
#   و اضافه می‌کنیم. هم فاصله و هم کاما پشتیبانی می‌شود:
#       /add user1 user2 user3
#       /add user1, user2, user3
# ============================================================

import re
import logging
from typing import List, Set
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    if not is_admin_chat(chat_id):
        return

    if not context.args:
        await update.message.reply_text(
            "❌ لطفا نام کاربری را وارد کنید.\n"
            "مثال: `/add elonmusk`\n"
            "برای چند اکانت: `/add user1 user2 user3`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ---- ترکیب همه آرگومان‌ها و جدا کردن با فاصله یا کاما ----
    raw = " ".join(context.args)
    raw_items = [u for u in re.split(r"[,\s]+", raw) if u]

    # پاک‌سازی + حذف تکراری‌ها (با حفظ ترتیب)
    seen: Set[str] = set()
    clean_list: List[str] = []
    for u in raw_items:
        c = clean_username(u)
        if c and c not in seen:
            seen.add(c)
            clean_list.append(c)

    if not clean_list:
        await update.message.reply_text("❌ نام کاربری وارد شده ساختار صحیحی ندارد.")
        return

    # ---- پیام «در حال بررسی» ----
    wait_msg = await update.message.reply_text(
        f"🔍 در حال بررسی و افزودن {len(clean_list)} اکانت...",
        parse_mode=ParseMode.MARKDOWN,
    )

    added: List[str] = []
    failed: List[str] = []
    skipped: List[str] = []

    for username in clean_list:
        # اعتبارسنجی فرمت
        if not valid_username(username):
            failed.append(username)
            continue

        # اگه از قبل اضافه شده
        if chat_has_username(chat_id, username):
            skipped.append(username)
            continue

        # بررسی فید RSS (این بخش همان بخش قبلی‌ست ولی داخل حلقه)
        try:
            feed = await fetch_rss_feed(username)
            if not feed or not feed.entries:
                failed.append(username)
                continue

            last_id = extract_tweet_id(feed.entries[0])
            add_chat_to_username(chat_id, username, last_id)
            added.append(username)
        except Exception as e:
            logger.warning(f"add failed for {username}: {e}")
            failed.append(username)

    # فقط یک‌بار فایل ذخیره شود
    if added:
        save_tracked()

    # ---- ساخت پیام نتیجه ----
    parts: List[str] = []
    parts.append(f"✅ {len(added)} اکانت با موفقیت افزوده شد.")
    if added:
        parts.append("\n".join(f"➕ `@{u}`" for u in added))
    if skipped:
        parts.append(
            f"⚠️ {len(skipped)} اکانت از قبل موجود بود:\n"
            + " ".join(f"`@{u}`" for u in skipped)
        )
    if failed:
        parts.append(
            f"❌ {len(failed)} اکانت نامعتبر یا فیدش یافت نشد:\n"
            + " ".join(f"`@{u}`" for u in failed)
        )

    result_msg = "\n\n".join(parts)
    if len(result_msg) > 4000:
        result_msg = result_msg[:4000] + "\n…"
    await wait_msg.edit_text(result_msg, parse_mode=ParseMode.MARKDOWN)
