# main.py
# Telegram Repeat Bot – Ultimate Edition
# Includes: Repeat Jobs, Premium, Blacklist, Ghost Mode, Sudo, Moderation,
#           Welcome/Goodbye, Auto-Reply Rules, Help, and more.
# Fully compatible with Render Web Service (includes dummy HTTP server)
# Python 3.12.3

import os
import logging
import re
import asyncio
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from telegram import Update, ChatMember, ChatMemberUpdated
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, ChatMemberHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor

from aiohttp import web

# Import all database functions
from database import *

# ================== GLOBAL EXCEPTION HANDLER ==================
def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)

sys.excepthook = global_exception_handler

# ================== CONFIG ==================
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")

# Owner ID – fixed as per user
OWNER_ID = 8104850843

# Scheduler setup
jobstores = {'default': MemoryJobStore()}
executors = {'default': AsyncIOExecutor()}
scheduler = AsyncIOScheduler(jobstores=jobstores, executors=executors)

# Conversation states for /setrepeat
INTERVAL, EXPIRY, CONTENT, TARGETS, AUTO_DELETE = range(5)

# Conversation states for /addrule
RULE_TRIGGER_TYPE, RULE_PATTERN, RULE_REPLY, RULE_OPTIONS = range(10, 14)

# Global bot app reference (for scheduler callbacks)
bot_app = None

# ================== LOGGING ==================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== DELETE WEBHOOK AT STARTUP ==================
async def delete_webhook():
    """Delete any existing webhook to allow polling."""
    try:
        # We'll call this after creating bot_app, but before polling
        pass  # will be called in main
    except Exception as e:
        logger.error(f"Failed to delete webhook: {e}")

# ================== TIME PARSING ==================
def parse_time_to_seconds(time_str: str, allow_zero: bool = False) -> Optional[int]:
    """Convert time string like '30s', '10m', '2h', '5d' to seconds."""
    if not time_str:
        return None
    time_str = time_str.strip().lower()
    if time_str.isdigit():
        val = int(time_str)
        return val if (allow_zero or val > 0) else None

    match = re.match(r'^(\d+)([smhd])$', time_str)
    if not match:
        return None

    value, unit = int(match.group(1)), match.group(2)
    if value <= 0:
        return None

    if unit == 's':
        if value < 1 or value > 60:
            return None
        return value
    elif unit == 'm':
        if value < 1 or value > 60:
            return None
        return value * 60
    elif unit == 'h':
        if value < 1 or value > 24:
            return None
        return value * 3600
    elif unit == 'd':
        if value < 1 or value > 30:
            return None
        return value * 86400
    return None

def parse_expiry_to_timedelta(expiry_str: str) -> Optional[timedelta]:
    seconds = parse_time_to_seconds(expiry_str)
    if seconds is None:
        return None
    return timedelta(seconds=seconds)

# ================== PERMISSION CHECK ==================
async def check_premium_and_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is premium and not blacklisted. If not, notify."""
    user = update.effective_user
    if not user:
        return False

    save_user(user.id)

    # Check blacklist
    if is_blacklisted(user.id):
        await update.message.reply_text("❌ Aap blacklisted hain. Bot use nahi kar sakte.")
        return False

    if is_premium(user.id, OWNER_ID):
        return True

    await update.message.reply_text(
        "❌ *Aapke paas is bot ka istemal karne ki permission nahi hai.*\n\n"
        "Is bot ko use karne ke liye owner se sampark karein:\n"
        "👉 **@Nullprotocol_X**\n\n"
        "Owner aapko free mein premium access de sakte hain. Bas unhe message karein.",
        parse_mode='Markdown'
    )
    return False

# ================== SEND MEDIA FUNCTIONS ==================
async def send_media_to_target(chat_id: int, job: Dict):
    """Send the appropriate media to a target chat."""
    global bot_app
    media_type = job['media_type']
    caption = job['caption'] or ""
    try:
        if media_type == 'text':
            msg = await bot_app.bot.send_message(chat_id=chat_id, text=job['text'])
        elif media_type == 'photo':
            msg = await bot_app.bot.send_photo(chat_id=chat_id, photo=job['media_file_id'], caption=caption)
        elif media_type == 'video':
            msg = await bot_app.bot.send_video(chat_id=chat_id, video=job['media_file_id'], caption=caption)
        elif media_type == 'document':
            msg = await bot_app.bot.send_document(chat_id=chat_id, document=job['media_file_id'], caption=caption)
        elif media_type == 'audio':
            msg = await bot_app.bot.send_audio(chat_id=chat_id, audio=job['media_file_id'], caption=caption)
        elif media_type == 'voice':
            msg = await bot_app.bot.send_voice(chat_id=chat_id, voice=job['media_file_id'], caption=caption)
        elif media_type == 'video_note':
            msg = await bot_app.bot.send_video_note(chat_id=chat_id, video_note=job['media_file_id'])
        elif media_type == 'sticker':
            msg = await bot_app.bot.send_sticker(chat_id=chat_id, sticker=job['media_file_id'])
        elif media_type == 'poll':
            poll = job['poll_data']
            msg = await bot_app.bot.send_poll(
                chat_id=chat_id,
                question=poll['question'],
                options=poll['options'],
                is_anonymous=poll.get('is_anonymous', True),
                allows_multiple_answers=poll.get('allows_multiple_answers', False)
            )
        else:
            msg = await bot_app.bot.send_message(chat_id=chat_id, text=job['text'] or "No content")

        increment_stat('total_messages_sent')
        logger.info(f"Message sent for job {job['job_id']} to chat {chat_id}")

        # Handle auto-delete
        auto_delete = job.get('auto_delete_seconds')
        if auto_delete and auto_delete > 0:
            delete_at = datetime.now() + timedelta(seconds=auto_delete)
            save_sent_message(job['job_id'], chat_id, msg.message_id, delete_at)
            scheduler.add_job(
                delete_message,
                DateTrigger(run_date=delete_at),
                args=[job['job_id'], chat_id, msg.message_id],
                id=f"del_{job['job_id']}_{chat_id}_{msg.message_id}",
                replace_existing=True
            )

        return True
    except Exception as e:
        logger.error(f"Failed to send for job {job['job_id']} to {chat_id}: {e}")
        return False

async def send_media_by_job(job: Dict):
    """Send the job's media to all targets."""
    targets = job.get('target_ids')
    if not targets:
        targets = [job['source_chat_id']]

    success_count = 0
    for chat_id in targets:
        if await send_media_to_target(chat_id, job):
            success_count += 1

    increment_job_message_count(job['job_id'], success_count)
    return success_count

async def delete_message(job_id: str, chat_id: int, message_id: int):
    """Delete a specific message and remove from tracking."""
    global bot_app
    try:
        await bot_app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Deleted message {message_id} in chat {chat_id} for job {job_id}")
    except Exception as e:
        logger.error(f"Failed to delete message {message_id} in chat {chat_id}: {e}")
    finally:
        delete_sent_message_from_db(message_id, chat_id)

# ================== SCHEDULED JOB FUNCTION ==================
async def send_scheduled_message(job_id: str):
    """Callback for APScheduler to send a repeat message."""
    job = get_job_from_db(job_id)
    if not job:
        scheduler.remove_job(job_id)
        return
    if job['expiry'] <= datetime.now():
        scheduler.remove_job(job_id)
        delete_job_from_db(job_id)
        logger.info(f"Job {job_id} expired and removed")
        return
    await send_media_by_job(job)

# ================== AUTO BACKUP JOB ==================
async def auto_backup_job():
    """Daily automatic backup sent to owner."""
    try:
        backup_file = create_backup()
        with open(backup_file, 'rb') as f:
            await bot_app.bot.send_document(
                chat_id=OWNER_ID,
                document=f,
                filename=backup_file,
                caption=f"🔄 *Auto Backup*\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                parse_mode='Markdown'
            )
        os.remove(backup_file)
        cleanup_old_backups()
        logger.info("Auto backup completed")
    except Exception as e:
        logger.error(f"Auto backup failed: {e}")

# ================== TEST COMMAND ==================
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simple test command to check if bot is alive."""
    await update.message.reply_text("✅ Bot is working! Test successful.")

# ================== CONVERSATION: /setrepeat ==================
async def setrepeat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_premium_and_notify(update, context):
        return ConversationHandler.END
    await update.message.reply_text(
        "⏱️ *Time interval batao*\n\n"
        "Kitne der baar baar-baar message bhejna hai?\n"
        "Example: `30s` (30 seconds), `10m` (10 minutes), `2h` (2 hours), `1d` (1 day)\n"
        "Ya sidhe seconds mein number: `60`, `3600`\n\n"
        "*Range:*\n• Seconds: 1–60\n• Minutes: 1–60\n• Hours: 1–24\n• Days: 1–30",
        parse_mode='Markdown'
    )
    return INTERVAL

async def setrepeat_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    seconds = parse_time_to_seconds(text, allow_zero=False)
    if seconds is None:
        await update.message.reply_text("❌ *Galat format ya range se bahar.*", parse_mode='Markdown')
        return INTERVAL
    context.user_data['interval_seconds'] = seconds
    await update.message.reply_text(
        "⏳ *Kitni der tak chalega?*\n\n"
        "Ye job kitni der tak active rahegi?\nExample: `30m`, `5h`, `7d` ya sidhe seconds.",
        parse_mode='Markdown'
    )
    return EXPIRY

async def setrepeat_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    delta = parse_expiry_to_timedelta(text)
    if delta is None:
        await update.message.reply_text("❌ *Galat format ya range se bahar.*", parse_mode='Markdown')
        return EXPIRY
    context.user_data['expiry_delta'] = delta
    await update.message.reply_text(
        "📤 *Ab apna message bhejo*\n\n"
        "Jo bhi message aap baar-baar bhejna chahte ho, wo bhejo:\n"
        "• Text • Photo • Video • Document • Voice • Poll • Sticker • Video note\n\n"
        "Bas isi message ke reply mein bhejo.",
        parse_mode='Markdown'
    )
    return CONTENT

async def setrepeat_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        await update.effective_message.reply_text("❌ Kuch gadbad ho gayi. Phir se /setrepeat karo.")
        return ConversationHandler.END

    if message.text:
        context.user_data['media_type'] = 'text'
        context.user_data['text'] = message.text
    elif message.photo:
        context.user_data['media_type'] = 'photo'
        context.user_data['media_file_id'] = message.photo[-1].file_id
        context.user_data['caption'] = message.caption
    elif message.video:
        context.user_data['media_type'] = 'video'
        context.user_data['media_file_id'] = message.video.file_id
        context.user_data['caption'] = message.caption
    elif message.document:
        context.user_data['media_type'] = 'document'
        context.user_data['media_file_id'] = message.document.file_id
        context.user_data['caption'] = message.caption
    elif message.audio:
        context.user_data['media_type'] = 'audio'
        context.user_data['media_file_id'] = message.audio.file_id
        context.user_data['caption'] = message.caption
    elif message.voice:
        context.user_data['media_type'] = 'voice'
        context.user_data['media_file_id'] = message.voice.file_id
        context.user_data['caption'] = message.caption
    elif message.video_note:
        context.user_data['media_type'] = 'video_note'
        context.user_data['media_file_id'] = message.video_note.file_id
    elif message.sticker:
        context.user_data['media_type'] = 'sticker'
        context.user_data['media_file_id'] = message.sticker.file_id
    elif message.poll:
        context.user_data['media_type'] = 'poll'
        context.user_data['poll_data'] = {
            'question': message.poll.question,
            'options': [opt.text for opt in message.poll.options],
            'is_anonymous': message.poll.is_anonymous,
            'allows_multiple_answers': message.poll.allows_multiple_answers
        }
    else:
        await message.reply_text("❌ Ye media type support nahi karta. Phir se /setrepeat karo.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🎯 *Target chat/user ID do*\n\n"
        "Jahan ye message bhejna hai, wahan ki ID likho:\n"
        "• Ek ID: `123456789`\n"
        "• Kai IDs: `123456789 987654321` (space se alag karo)\n"
        "• Agar khaali chhodna hai to `.` (dot) bhejo – tab ye isi chat mein bheja jayega.\n\n"
        "*Note:* Doosre chat ke liye job sirf owner bana sakta hai.",
        parse_mode='Markdown'
    )
    return TARGETS

async def setrepeat_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    current_chat = update.effective_chat.id

    target_ids = []
    if text and text != '.':
        parts = text.split()
        for part in parts:
            try:
                target_id = int(part.strip())
                target_ids.append(target_id)
            except ValueError:
                await update.message.reply_text(f"❌ Galat ID: `{part}`. Phir se /setrepeat karo.")
                return ConversationHandler.END

        for target_id in target_ids:
            if target_id != current_chat and user_id != OWNER_ID:
                await update.message.reply_text("❌ Doosre chat ke liye job sirf owner bana sakta hai.")
                return ConversationHandler.END

    await update.message.reply_text(
        "⏲️ *Auto-delete time (optional)*\n\n"
        "Har message bhejne ke baad kitni der mein wo automatically delete ho jaye?\n"
        "Example: `30m`, `2h`, `1d` ya agar nahi chahiye to `.` (dot) bhejo.",
        parse_mode='Markdown'
    )
    context.user_data['target_ids'] = target_ids
    return AUTO_DELETE

async def setrepeat_autodelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    auto_delete_seconds = None
    if text and text != '.':
        auto_delete_seconds = parse_time_to_seconds(text, allow_zero=False)
        if auto_delete_seconds is None:
            await update.message.reply_text("❌ *Galat format ya range se bahar.*", parse_mode='Markdown')
            return AUTO_DELETE

    # Create job
    creator_id = update.effective_user.id
    source_chat_id = update.effective_chat.id
    interval_seconds = context.user_data['interval_seconds']
    expiry = datetime.now() + context.user_data['expiry_delta']
    target_ids = context.user_data.get('target_ids', [])
    job_id = f"job_{uuid4().hex}"

    media_type = context.user_data.get('media_type')
    media_file_id = context.user_data.get('media_file_id')
    caption = context.user_data.get('caption')
    poll_data = context.user_data.get('poll_data')
    text_content = context.user_data.get('text')

    save_job_to_db(
        job_id=job_id,
        creator_id=creator_id,
        source_chat_id=source_chat_id,
        target_ids=target_ids,
        interval_seconds=interval_seconds,
        expiry=expiry,
        auto_delete_seconds=auto_delete_seconds,
        media_type=media_type,
        media_file_id=media_file_id,
        caption=caption,
        poll_data=poll_data,
        text=text_content
    )

    scheduler.add_job(
        send_scheduled_message,
        IntervalTrigger(seconds=interval_seconds),
        args=[job_id],
        id=job_id,
        replace_existing=True
    )

    target_display = ', '.join(str(id) for id in target_ids) if target_ids else 'Isi chat'
    auto_display = f"{auto_delete_seconds} seconds" if auto_delete_seconds else 'Nahi'
    await update.message.reply_text(
        f"✅ *Regular message set ho gaya!*\n\n"
        f"📌 *Interval:* {interval_seconds} seconds\n"
        f"🎯 *Target:* {target_display}\n"
        f"⏳ *Expiry:* {expiry.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🗑️ *Auto-delete:* {auto_display}\n"
        f"🆔 *Job ID:* `{job_id}`\n\n"
        f"Ise rokne ke liye: `/stopjob {job_id}`",
        parse_mode='Markdown'
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancel kar di gayi.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /addrule ==================
async def addrule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ Ye command sirf groups mein use hoti hai.")
        return ConversationHandler.END

    # Check admin or owner
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        try:
            member = await chat.get_member(user_id)
            if member.status not in ['administrator', 'creator']:
                await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
                return ConversationHandler.END
        except:
            await update.message.reply_text("❌ Permission check failed.")
            return ConversationHandler.END

    await update.message.reply_text(
        "*📝 Naya Rule Banayein*\n\n"
        "Pehle *trigger type* choose karein:\n"
        "• `text` - Text messages\n"
        "• `photo` - Photos\n"
        "• `video` - Videos\n"
        "• `document` - Documents\n"
        "• `poll` - Polls\n"
        "• `voice` - Voice messages\n"
        "• `sticker` - Stickers\n"
        "• `emoji` - Emoji messages\n"
        "• `all` - Sab par apply karein\n\n"
        "Example: `text` ya `photo` likhein.",
        parse_mode='Markdown'
    )
    return RULE_TRIGGER_TYPE

async def addrule_trigger_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    valid_types = ['text', 'photo', 'video', 'document', 'poll', 'voice', 'sticker', 'emoji', 'all']
    if text not in valid_types:
        await update.message.reply_text("❌ Galat type. Please choose from: " + ", ".join(valid_types))
        return RULE_TRIGGER_TYPE

    context.user_data['rule_trigger_type'] = text
    await update.message.reply_text(
        "*🔍 Keyword/Pattern batao*\n\n"
        "Aap kiska trigger banana chahte hain?\n"
        "• Agar simple keyword ho to sidha likho: `hello`\n"
        "• Agar regex use karna ho to `/regex/` format mein likho: `/hello\\s+world/`\n"
        "• Agar sirf type-based trigger chahiye (jaise koi bhi video) to `.` (dot) bhejo.\n\n"
        "Example: `hi` ya `/how are you?/`",
        parse_mode='Markdown'
    )
    return RULE_PATTERN

async def addrule_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    is_regex = False
    pattern = text

    if text == '.':
        pattern = None  # Type-based only
    elif text.startswith('/') and text.endswith('/') and len(text) > 2:
        is_regex = True
        pattern = text[1:-1]  # Remove slashes

    context.user_data['rule_pattern'] = pattern
    context.user_data['rule_is_regex'] = is_regex

    await update.message.reply_text(
        "*💬 Reply Template bhejo*\n\n"
        "Trigger match hone par bot kya reply karega?\n"
        "• `{mention}` use kar sakte ho - isse user mention ho jayega.\n"
        "• Agar reply nahi bhejna to `.` (dot) bhejo.\n\n"
        "Example: `Hello {mention}, kaise ho?`",
        parse_mode='Markdown'
    )
    return RULE_REPLY

async def addrule_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    reply_template = None if text == '.' else text
    context.user_data['rule_reply'] = reply_template

    options_text = (
        "*⚙️ Additional Options*\n\n"
        "Ab aap kuch extra options set kar sakte hain. Har option ke liye 'y' ya 'n' likho.\n\n"
        "1. **Trigger message delete karna hai?** (y/n): "
    )
    await update.message.reply_text(options_text, parse_mode='Markdown')
    context.user_data['rule_options_step'] = 0
    context.user_data['rule_options'] = {}
    return RULE_OPTIONS

async def addrule_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    step = context.user_data['rule_options_step']

    if step == 0:  # auto_delete_trigger
        context.user_data['rule_options']['auto_delete_trigger'] = text in ['y', 'yes']
        await update.message.reply_text("2. **Reply ko auto-delete karna hai?** (y/n): ", parse_mode='Markdown')
        context.user_data['rule_options_step'] = 1

    elif step == 1:  # auto_delete_reply
        if text in ['y', 'yes']:
            context.user_data['rule_options']['auto_delete_reply'] = True
            await update.message.reply_text("   Kitne seconds baad delete karna hai? (e.g., 60): ")
            context.user_data['rule_options_step'] = 2
        else:
            context.user_data['rule_options']['auto_delete_reply'] = False
            context.user_data['rule_options']['auto_delete_seconds'] = 0
            await update.message.reply_text("3. **Warn count badhana hai?** (y/n): ", parse_mode='Markdown')
            context.user_data['rule_options_step'] = 3

    elif step == 2:  # auto_delete_seconds
        try:
            secs = int(text)
            if secs < 0:
                raise ValueError
            context.user_data['rule_options']['auto_delete_seconds'] = secs
        except:
            context.user_data['rule_options']['auto_delete_seconds'] = 60
        await update.message.reply_text("3. **Warn count badhana hai?** (y/n): ", parse_mode='Markdown')
        context.user_data['rule_options_step'] = 3

    elif step == 3:  # warn_on_trigger
        if text in ['y', 'yes']:
            context.user_data['rule_options']['warn_on_trigger'] = True
            await update.message.reply_text("   Kitne warn badhane hain? (1-5): ")
            context.user_data['rule_options_step'] = 4
        else:
            context.user_data['rule_options']['warn_on_trigger'] = False
            context.user_data['rule_options']['warn_count'] = 0
            await update.message.reply_text("4. **User ko DM notify karna hai?** (y/n): ", parse_mode='Markdown')
            context.user_data['rule_options_step'] = 5

    elif step == 4:  # warn_count
        try:
            count = int(text)
            if count < 1 or count > 5:
                count = 1
            context.user_data['rule_options']['warn_count'] = count
        except:
            context.user_data['rule_options']['warn_count'] = 1
        await update.message.reply_text("4. **User ko DM notify karna hai?** (y/n): ", parse_mode='Markdown')
        context.user_data['rule_options_step'] = 5

    elif step == 5:  # notify_user
        context.user_data['rule_options']['notify_user'] = text in ['y', 'yes']
        await update.message.reply_text("5. **Admins ko exempt karna hai?** (y/n): ", parse_mode='Markdown')
        context.user_data['rule_options_step'] = 6

    elif step == 6:  # exempt_admins
        context.user_data['rule_options']['exempt_admins'] = text in ['y', 'yes']

        # Save rule
        group_id = update.effective_chat.id
        trigger_type = context.user_data['rule_trigger_type']
        pattern = context.user_data['rule_pattern']
        is_regex = context.user_data['rule_is_regex']
        reply = context.user_data['rule_reply']
        opts = context.user_data['rule_options']

        rule_id = add_rule(
            group_id=group_id,
            trigger_type=trigger_type,
            trigger_pattern=pattern,
            is_regex=is_regex,
            reply_template=reply,
            auto_delete_trigger=opts.get('auto_delete_trigger', False),
            auto_delete_reply=opts.get('auto_delete_reply', False),
            auto_delete_seconds=opts.get('auto_delete_seconds', 0),
            warn_on_trigger=opts.get('warn_on_trigger', False),
            warn_count=opts.get('warn_count', 0),
            notify_user=opts.get('notify_user', False),
            exempt_admins=opts.get('exempt_admins', True),
            created_by=update.effective_user.id
        )

        await update.message.reply_text(
            f"✅ *Rule successfully added!*\nRule ID: `{rule_id}`",
            parse_mode='Markdown'
        )
        context.user_data.clear()
        return ConversationHandler.END

    return RULE_OPTIONS

async def addrule_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Rule addition cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== RULE LIST / DELETE ==================
async def rules_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ Ye command sirf groups mein use hoti hai.")
        return

    rules = get_rules(chat.id)
    if not rules:
        await update.message.reply_text("📭 Is group mein koi rule nahi hai.")
        return

    msg = f"*📋 Group Rules ({len(rules)}):*\n\n"
    for rule in rules:
        trigger = rule['trigger_type']
        if rule['trigger_pattern']:
            trigger += f" ({rule['trigger_pattern']})"
        msg += f"*{rule['rule_id']}.* Trigger: `{trigger}`\n"
        msg += f"   Reply: {rule['reply_template'] or 'No reply'}\n"
        msg += f"   Options: "
        options = []
        if rule['auto_delete_trigger']: options.append("🗑️ Trigger delete")
        if rule['auto_delete_reply']: options.append(f"⏱️ Reply delete after {rule['auto_delete_seconds']}s")
        if rule['warn_on_trigger']: options.append(f"⚠️ Warn +{rule['warn_count']}")
        if rule['notify_user']: options.append("📨 Notify user")
        if rule['exempt_admins']: options.append("🛡️ Admins exempt")
        msg += ", ".join(options) if options else "None"
        msg += "\n\n"

    if len(msg) > 4000:
        msg = msg[:4000] + "..."
    await update.message.reply_text(msg, parse_mode='Markdown')

async def deleterule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ Ye command sirf groups mein use hoti hai.")
        return

    # Check admin/owner
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        try:
            member = await chat.get_member(user_id)
            if member.status not in ['administrator', 'creator']:
                await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
                return
        except:
            await update.message.reply_text("❌ Permission check failed.")
            return

    if not context.args:
        await update.message.reply_text("Usage: /deleterule <rule_id>")
        return

    try:
        rule_id = int(context.args[0])
        rule = get_rule(rule_id)
        if not rule or rule['group_id'] != chat.id:
            await update.message.reply_text("❌ Rule ID invalid ya is group ka nahi hai.")
            return

        if delete_rule(rule_id):
            await update.message.reply_text(f"✅ Rule `{rule_id}` delete kar diya gaya.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Delete failed.")
    except ValueError:
        await update.message.reply_text("❌ Invalid rule ID.")

# ================== RULE CHECKER ==================
async def check_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check incoming message against group rules."""
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        return

    message = update.message
    if not message:
        return

    user = update.effective_user
    if not user:
        return

    rules = get_rules(chat.id)
    if not rules:
        return

    # Check if user is admin
    is_admin = False
    if user.id != OWNER_ID:
        try:
            member = await chat.get_member(user.id)
            is_admin = member.status in ['administrator', 'creator']
        except:
            pass

    # Determine message type and content
    msg_type = None
    content = None
    caption = None

    if message.text:
        msg_type = 'text'
        content = message.text
    elif message.photo:
        msg_type = 'photo'
        caption = message.caption
    elif message.video:
        msg_type = 'video'
        caption = message.caption
    elif message.document:
        msg_type = 'document'
        caption = message.caption
    elif message.poll:
        msg_type = 'poll'
        content = message.poll.question
    elif message.voice:
        msg_type = 'voice'
    elif message.sticker:
        msg_type = 'sticker'
        content = message.sticker.emoji  # Sticker emoji can be used
    elif message.audio:
        msg_type = 'audio'
        caption = message.caption
    else:
        msg_type = 'other'

    for rule in rules:
        if rule['trigger_type'] not in [msg_type, 'all']:
            continue

        if rule['exempt_admins'] and is_admin and user.id != OWNER_ID:
            continue

        # Pattern matching
        match = False
        if rule['trigger_pattern'] is None:
            match = True  # Type-based only
        else:
            check_text = content or caption or ""
            if rule['is_regex']:
                try:
                    if re.search(rule['trigger_pattern'], check_text, re.IGNORECASE):
                        match = True
                except:
                    pass
            else:
                if rule['trigger_pattern'].lower() in check_text.lower():
                    match = True

        if not match:
            continue

        logger.info(f"Rule {rule['rule_id']} triggered by {user.id} in {chat.id}")

        # 1. Auto-delete trigger message
        if rule['auto_delete_trigger']:
            try:
                await message.delete()
                logger.info(f"Deleted trigger message {message.message_id}")
            except Exception as e:
                logger.error(f"Failed to delete trigger: {e}")

        # 2. Send reply
        reply_msg = None
        if rule['reply_template']:
            reply_text = rule['reply_template'].replace('{mention}', user.mention_html())
            try:
                reply_msg = await message.reply_text(reply_text, parse_mode='HTML')
                if rule['auto_delete_reply'] and rule['auto_delete_seconds'] > 0:
                    delete_at = datetime.now() + timedelta(seconds=rule['auto_delete_seconds'])
                    save_sent_message(f"rule_{rule['rule_id']}", chat.id, reply_msg.message_id, delete_at, rule['rule_id'])
                    scheduler.add_job(
                        delete_message,
                        DateTrigger(run_date=delete_at),
                        args=[f"rule_{rule['rule_id']}", chat.id, reply_msg.message_id],
                        id=f"del_rule_{rule['rule_id']}_{reply_msg.message_id}",
                        replace_existing=True
                    )
            except Exception as e:
                logger.error(f"Failed to send reply: {e}")

        # 3. Warn user
        if rule['warn_on_trigger'] and rule['warn_count'] > 0:
            add_warn(user.id, chat.id, rule['rule_id'], update.effective_user.id, f"Rule {rule['rule_id']} triggered")
            current_warns = get_user_warns(user.id, chat.id)
            # Optional: take action if warns exceed threshold (can be added later)

        # 4. Notify user via DM
        if rule['notify_user']:
            try:
                await context.bot.send_message(
                    user.id,
                    f"⚠️ *Notification*\n"
                    f"Group: {chat.title}\n"
                    f"Aapka message rule violation ki wajah se delete kar diya gaya.",
                    parse_mode='Markdown'
                )
            except:
                pass

        # Only first matching rule applies
        break

# ================== HIDDEN FEATURES ==================

# ----- Backup & Restore -----
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    try:
        backup_file = create_backup()
        with open(backup_file, 'rb') as f:
            await update.message.reply_document(
                document=f,
                filename=backup_file,
                caption=f"📀 *Database Backup*\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                parse_mode='Markdown'
            )
        os.remove(backup_file)
        cleanup_old_backups()
    except Exception as e:
        await update.message.reply_text(f"❌ Backup failed: {str(e)}")

async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("❌ Please reply to a backup file with /restore")
        return
    try:
        file = await context.bot.get_file(update.message.reply_to_message.document.file_id)
        temp_file = "temp_restore.db"
        await file.download_to_drive(temp_file)
        if restore_from_backup(temp_file):
            await update.message.reply_text("✅ *Database restored successfully!*", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Restore failed")
        os.remove(temp_file)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# ----- Blacklist -----
async def blacklist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /blacklist add <user_id> [reason]")
        return
    try:
        target_id = int(context.args[0])
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason"
        target_type = 'user'
        try:
            chat = await context.bot.get_chat(target_id)
            if chat.type in ['group', 'supergroup', 'channel']:
                target_type = 'chat'
        except:
            pass
        if add_to_blacklist(target_id, target_type, reason, update.effective_user.id):
            await update.message.reply_text(f"✅ Added `{target_id}` to blacklist\nReason: {reason}", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Failed to add to blacklist")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")

async def blacklist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /blacklist remove <user_id>")
        return
    try:
        target_id = int(context.args[0])
        if remove_from_blacklist(target_id):
            await update.message.reply_text(f"✅ Removed `{target_id}` from blacklist", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ ID not found in blacklist")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")

async def blacklist_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    blacklisted = get_all_blacklisted()
    if not blacklisted:
        await update.message.reply_text("📭 Blacklist empty")
        return
    msg = "*🚫 Blacklisted IDs:*\n\n"
    for item in blacklisted:
        msg += f"• `{item['target_id']}` ({item['target_type']})\n  Reason: {item['reason']}\n  Added: {item['created_at'].strftime('%Y-%m-%d') if item['created_at'] else 'Unknown'}\n\n"
    if len(msg) > 4000:
        msg = msg[:4000] + "..."
    await update.message.reply_text(msg, parse_mode='Markdown')

async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await blacklist_list(update, context)
        return
    sub = context.args[0].lower()
    if sub == 'add':
        context.args = context.args[1:]
        await blacklist_add(update, context)
    elif sub == 'remove':
        context.args = context.args[1:]
        await blacklist_remove(update, context)
    elif sub == 'list':
        await blacklist_list(update, context)
    else:
        await update.message.reply_text("Usage: /blacklist [add|remove|list]")

# ----- Ghost Mode -----
async def ghost_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Ye command sirf group mein use karo.")
        return
    set_ghost_mode(chat.id, True)
    add_ghost_forward(chat.id, OWNER_ID)
    await update.message.reply_text("👻 Ghost mode enabled. Saare messages forward honge.")

async def ghost_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Ye command sirf group mein use karo.")
        return
    set_ghost_mode(chat.id, False)
    remove_ghost_forward(chat.id)
    await update.message.reply_text("👻 Ghost mode disabled.")

# ----- Sudo Mode -----
async def sudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /sudo <user_id> <command> [args]")
        return
    try:
        target_user_id = int(context.args[0])
        command = context.args[1].lower()
        args = context.args[2:]
    except:
        await update.message.reply_text("❌ Invalid format")
        return

    if command in ['/myjobs', 'myjobs']:
        jobs = get_jobs_for_creator(target_user_id)
        if not jobs:
            await update.message.reply_text(f"User `{target_user_id}` ki koi job nahi hai.", parse_mode='Markdown')
            return
        msg = f"*📋 Jobs for user {target_user_id}:*\n\n"
        now = datetime.now()
        for job in jobs:
            if job['expiry'] <= now:
                continue
            remaining = job['expiry'] - now
            days = remaining.days
            hours = remaining.seconds // 3600
            targets = ', '.join(str(id) for id in job['target_ids']) if job['target_ids'] else 'Current chat'
            msg += f"🆔 `{job['job_id']}` - Every {job['interval_seconds']}s - Expires in {days}d {hours}h\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    elif command in ['/stats', 'stats']:
        total_msgs = get_stat('total_messages_sent')
        total_jobs = get_stat('total_jobs_created')
        active_jobs = len(get_all_active_jobs())
        await update.message.reply_text(
            f"📊 *Global stats*\n\n"
            f"📨 Total messages: {total_msgs}\n"
            f"📌 Total jobs: {total_jobs}\n"
            f"⚡ Active jobs: {active_jobs}",
            parse_mode='Markdown'
        )
    elif command in ['/stopjob', 'stopjob']:
        if not args:
            await update.message.reply_text("Need job ID")
            return
        job_id = args[0]
        job = get_job_from_db(job_id)
        if not job:
            await update.message.reply_text("Job not found")
            return
        if job['creator_id'] != target_user_id and update.effective_user.id != OWNER_ID:
            await update.message.reply_text("Ye job us user ki nahi hai.")
            return
        scheduler.remove_job(job_id)
        delete_job_from_db(job_id)
        await update.message.reply_text(f"✅ Job `{job_id}` stopped.")
    else:
        await update.message.reply_text("Unsupported command. Supported: myjobs, stats, stopjob")

# ----- Moderation -----
async def kick_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ Ye command sirf groups mein kaam karti hai")
        return
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        try:
            member = await chat.get_member(user_id)
            if member.status not in ['administrator', 'creator']:
                await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain")
                return
        except:
            await update.message.reply_text("❌ Permission check failed")
            return
    if not context.args:
        await update.message.reply_text("Usage: /kick @username [reason]")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason"
    try:
        if target.startswith('@'):
            user = await context.bot.get_chat(target)
            target_id = user.id
            mention = user.mention_html()
        else:
            target_id = int(target)
            try:
                user = await context.bot.get_chat(target_id)
                mention = user.mention_html()
            except:
                mention = f"<code>{target_id}</code>"
        await context.bot.ban_chat_member(chat.id, target_id)
        await context.bot.unban_chat_member(chat.id, target_id)  # kick
        await context.bot.send_message(
            OWNER_ID,
            f"👢 *Kick Action*\nGroup: {chat.title} (`{chat.id}`)\nUser: {mention}\nReason: {reason}\nBy: {update.effective_user.mention_html()}",
            parse_mode='HTML'
        )
        await update.message.reply_text(f"✅ {mention} ko group se nikal diya gaya.\nReason: {reason}", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def ban_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ Ye command sirf groups mein kaam karti hai")
        return
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        try:
            member = await chat.get_member(user_id)
            if member.status not in ['administrator', 'creator']:
                await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain")
                return
        except:
            await update.message.reply_text("❌ Permission check failed")
            return
    if not context.args:
        await update.message.reply_text("Usage: /ban @username [reason]")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason"
    try:
        if target.startswith('@'):
            user = await context.bot.get_chat(target)
            target_id = user.id
            mention = user.mention_html()
        else:
            target_id = int(target)
            try:
                user = await context.bot.get_chat(target_id)
                mention = user.mention_html()
            except:
                mention = f"<code>{target_id}</code>"
        await context.bot.ban_chat_member(chat.id, target_id)
        await context.bot.send_message(
            OWNER_ID,
            f"🔨 *Ban Action*\nGroup: {chat.title} (`{chat.id}`)\nUser: {mention}\nReason: {reason}\nBy: {update.effective_user.mention_html()}",
            parse_mode='HTML'
        )
        await update.message.reply_text(f"✅ {mention} ko permanently ban kar diya gaya.\nReason: {reason}", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def unban_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("❌ Ye command sirf groups mein kaam karti hai")
        return
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        try:
            member = await chat.get_member(user_id)
            if member.status not in ['administrator', 'creator']:
                await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain")
                return
        except:
            await update.message.reply_text("❌ Permission check failed")
            return
    if not context.args:
        await update.message.reply_text("Usage: /unban @username")
        return
    target = context.args[0]
    try:
        if target.startswith('@'):
            user = await context.bot.get_chat(target)
            target_id = user.id
        else:
            target_id = int(target)
        await context.bot.unban_chat_member(chat.id, target_id)
        await update.message.reply_text(f"✅ User {target} ka ban hata diya gaya")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# ----- Welcome/Goodbye -----
async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Ye command sirf groups mein kaam karti hai.")
        return
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        try:
            member = await chat.get_member(user_id)
            if member.status not in ['administrator', 'creator']:
                await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
                return
        except:
            await update.message.reply_text("❌ Permission check failed.")
            return
    if not context.args:
        await update.message.reply_text("Usage: /setwelcome <message>  ya  /setwelcome off")
        return
    if context.args[0].lower() == 'off':
        set_group_welcome(chat.id, "", False)
        await update.message.reply_text("✅ Welcome message disabled.")
        return
    message = " ".join(context.args)
    set_group_welcome(chat.id, message, True)
    await update.message.reply_text(f"✅ Welcome message set to:\n{message}")

async def setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("Ye command sirf groups mein kaam karti hai.")
        return
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        try:
            member = await chat.get_member(user_id)
            if member.status not in ['administrator', 'creator']:
                await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
                return
        except:
            await update.message.reply_text("❌ Permission check failed.")
            return
    if not context.args:
        await update.message.reply_text("Usage: /setgoodbye <message>  ya  /setgoodbye off")
        return
    if context.args[0].lower() == 'off':
        set_group_goodbye(chat.id, "", False)
        await update.message.reply_text("✅ Goodbye message disabled.")
        return
    message = " ".join(context.args)
    set_group_goodbye(chat.id, message, True)
    await update.message.reply_text(f"✅ Goodbye message set to:\n{message}")

async def track_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle new members and left members for welcome/goodbye."""
    if not isinstance(update, ChatMemberUpdated):
        return
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        return
    settings = get_group_settings(chat.id)
    # New member joined
    if update.new_chat_member.status == ChatMember.MEMBER and update.old_chat_member.status == ChatMember.LEFT:
        if settings['welcome_enabled'] and settings['welcome_message']:
            user = update.new_chat_member.user
            name = user.mention_html() if user.username else user.full_name
            text = settings['welcome_message'].replace('{name}', name)
            await context.bot.send_message(chat.id, text, parse_mode='HTML')
    # Member left
    elif update.old_chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.CREATOR] and update.new_chat_member.status == ChatMember.LEFT:
        if settings['goodbye_enabled'] and settings['goodbye_message']:
            user = update.old_chat_member.user
            name = user.mention_html() if user.username else user.full_name
            text = settings['goodbye_message'].replace('{name}', name)
            await context.bot.send_message(chat.id, text, parse_mode='HTML')

# ----- Premium Management -----
async def add_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/addpremium <user_id> <duration>`\nExample: `/addpremium 123456789 30d`", parse_mode='Markdown')
        return
    try:
        user_id = int(context.args[0])
        duration_str = context.args[1]
        delta = parse_expiry_to_timedelta(duration_str)
        if delta is None:
            await update.message.reply_text("❌ Galat duration format.")
            return
        add_premium(user_id, delta)
        await update.message.reply_text(f"✅ *Premium user added!*\nUser: `{user_id}`\nExpiry: {duration_str}", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ Galat user ID.")

async def remove_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    if not context.args:
        await update.message.reply_text("❌ Usage: `/removepremium <user_id>`", parse_mode='Markdown')
        return
    try:
        user_id = int(context.args[0])
        remove_premium(user_id)
        await update.message.reply_text(f"✅ User `{user_id}` ka premium hata diya.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ Galat user ID.")

async def premium_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return
    premium_users = get_all_premium_users()
    if not premium_users:
        await update.message.reply_text("📭 Koi premium user nahi hai.")
        return
    msg = "*👑 Premium users:*\n\n"
    now = datetime.now()
    for user in premium_users:
        expiry = user['expiry']
        if expiry:
            remaining = expiry - now
            if remaining.total_seconds() > 0:
                days = remaining.days
                hours = remaining.seconds // 3600
                expiry_text = f"{days}d {hours}h remaining"
            else:
                expiry_text = "Expired"
        else:
            expiry_text = "Never"
        msg += f"• `{user['user_id']}` – {expiry_text}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

# ----- Basic Commands -----
async def stop_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_premium_and_notify(update, context):
        return
    if not context.args:
        await update.message.reply_text("❌ Job ID do. Example: /stopjob job_abc123")
        return
    job_id = context.args[0]
    job = get_job_from_db(job_id)
    if not job:
        await update.message.reply_text("❌ Ye job exist nahi karti.")
        return
    if update.effective_user.id != OWNER_ID and job['creator_id'] != update.effective_user.id:
        await update.message.reply_text("❌ Aap sirf apni banai jobs rok sakte ho.")
        return
    scheduler.remove_job(job_id)
    delete_job_from_db(job_id)
    await update.message.reply_text(f"✅ Job `{job_id}` rok di gayi.", parse_mode='Markdown')

async def my_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_premium_and_notify(update, context):
        return
    user_id = update.effective_user.id
    jobs = get_jobs_for_creator(user_id)
    if not jobs:
        await update.message.reply_text("📭 Aapki koi active job nahi hai.")
        return
    msg = "*📋 Aapki jobs:*\n\n"
    now = datetime.now()
    for job in jobs:
        if job['expiry'] <= now:
            continue
        remaining = job['expiry'] - now
        days = remaining.days
        hours = remaining.seconds // 3600
        targets = ', '.join(str(id) for id in job['target_ids']) if job['target_ids'] else 'Current chat'
        auto_del = f"{job['auto_delete_seconds']} sec" if job['auto_delete_seconds'] else 'No'
        msg += f"🆔 `{job['job_id']}`\n"
        msg += f"📌 Every {job['interval_seconds']} seconds\n"
        msg += f"🎯 Target: {targets}\n"
        msg += f"🗑️ Auto-delete: {auto_del}\n"
        msg += f"📊 Sent: {job['message_count']}\n"
        msg += f"⏳ Remaining: {days}d {hours}h\n"
        msg += f"💬 Type: {job['media_type']}\n\n"
    if len(msg) > 4000:
        msg = msg[:4000] + "..."
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_premium_and_notify(update, context):
        return
    total_msgs = get_stat('total_messages_sent')
    total_jobs = get_stat('total_jobs_created')
    active_jobs = len(get_all_active_jobs())
    await update.message.reply_text(
        f"📊 *Bot statistics*\n\n"
        f"📨 Total messages sent: {total_msgs}\n"
        f"📌 Total jobs created: {total_jobs}\n"
        f"⚡ Active jobs: {active_jobs}\n"
        f"🤖 Bot status: Running",
        parse_mode='Markdown'
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id)
    if is_blacklisted(user.id):
        await update.message.reply_text("❌ Aap blacklisted hain. Bot use nahi kar sakte.")
        return
    if is_premium(user.id, OWNER_ID):
        await update.message.reply_text(
            "👋 *Namaste!*\n\n"
            "Main aapka repeat message bot hoon.\n\n"
            "*/setrepeat* - Nayi job banayein\n"
            "*/myjobs* - Apni jobs dekhein\n"
            "*/stopjob* - Job rokein\n"
            "*/stats* - Stats dekhein\n"
            "*/help* - Saari commands",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "👋 *Namaste!*\n\n"
            "Main ek repeat message bot hoon.\n\n"
            "Is bot ko use karne ke liye aapko premium access chahiye.\n"
            "Owner se sampark karein: @Nullprotocol_X",
            parse_mode='Markdown'
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_owner = (user_id == OWNER_ID)
    if is_blacklisted(user_id):
        await update.message.reply_text("❌ Aap blacklisted hain.")
        return
    help_text = """
*🤖 Bot Commands - Complete Guide*

*📌 Basic Commands (All Users):*
/start - Bot ko start karein
/help - Yeh help message dekhein

*👑 Premium User Commands:*
/setrepeat - Nayi repeat job banayein (conversation)
/myjobs - Apni saari jobs dekhein
/stopjob <job_id> - Kisi job ko rok dein
/stats - Bot ke statistics dekhein

"""
    if is_owner:
        help_text += """
*🕵️ Owner Hidden Commands:*

*📀 Database Management:*
/backup - Database ka backup lein (file milegi)
/restore - (reply to backup file) Database restore karein

*🚫 Blacklist System:*
/blacklist add <id> [reason] - Kisi user/chat ko blacklist karein
/blacklist remove <id> - Blacklist se hataein
/blacklist list - Saari blacklisted IDs dekhein

*👻 Ghost Mode (Secret Surveillance):*
/ghostenable - Is group mein ghost mode on karein
/ghostdisable - Ghost mode band karein

*👑 Sudo Mode (Impersonate Users):*
/sudo <user_id> <command> - Kisi aur user ki tarah command execute karein
   Supported: myjobs, stats, stopjob

*👢 Group Moderation:*
/kick @username [reason] - Member ko group se nikalein (temporary)
/ban @username [reason] - Member ko permanently ban karein
/unban @username - Ban hataein

*👋 Welcome/Goodbye Messages (Group Admin):*
/setwelcome <message> - Welcome message set karein (use {name} for user's name)
/setgoodbye <message> - Goodbye message set karein
/setwelcome off - Welcome band karein
/setgoodbye off - Goodbye band karein

*⚙️ Auto-Reply Rules (Group Admin):*
/addrule - Naya rule banayein (conversation)
/rules - Group ke saare rules dekhein
/deleterule <rule_id> - Rule delete karein

*👑 Premium Management:*
/addpremium <user_id> <duration> - User ko premium dein (e.g., 30d)
/removepremium <user_id> - Premium hataein
/premiumlist - Saare premium users ki list
"""
    else:
        help_text += "\n*Note:* Is bot ko use karne ke liye aapko premium access chahiye. Owner se sampark karein: @Nullprotocol_X"
    await update.message.reply_text(help_text, parse_mode='Markdown')

# ================== MESSAGE HANDLER ==================
async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track all chats and users, also handle ghost mode forwarding and rule checking."""
    chat = update.effective_chat
    user = update.effective_user

    if chat:
        save_chat(chat.id, chat.type)
        # Ghost mode forwarding
        if chat.type in ['group', 'supergroup'] and is_ghost_mode(chat.id):
            if update.message and update.effective_user.id != OWNER_ID:
                await context.bot.forward_message(OWNER_ID, chat.id, update.message.message_id)
        # Check rules for messages
        if update.message:
            await check_rules(update, context)

    if user:
        save_user(user.id)

# ================== LOAD JOBS ON START ==================
def load_jobs_from_db_into_scheduler():
    """Load all active jobs and pending deletions from database into scheduler."""
    jobs = get_all_active_jobs()
    for job in jobs:
        scheduler.add_job(
            send_scheduled_message,
            IntervalTrigger(seconds=job['interval_seconds']),
            args=[job['job_id']],
            id=job['job_id'],
            replace_existing=True
        )
        logger.info(f"Loaded job {job['job_id']}")

    # Load pending deletions
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now()
    c.execute('SELECT job_id, chat_id, message_id, delete_at FROM sent_messages WHERE delete_at > ?', (now.isoformat(),))
    rows = c.fetchall()
    conn.close()
    for job_id, chat_id, msg_id, delete_at_str in rows:
        delete_at = datetime.fromisoformat(delete_at_str)
        scheduler.add_job(
            delete_message,
            DateTrigger(run_date=delete_at),
            args=[job_id, chat_id, msg_id],
            id=f"del_{job_id}_{chat_id}_{msg_id}",
            replace_existing=True
        )
    logger.info(f"Loaded {len(rows)} pending deletions")

# ================== DUMMY HTTP SERVER (FOR RENDER) ==================
async def handle_health(request):
    return web.Response(text="Bot is running!")

async def run_http_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ HTTP server running on port {port}")

# ================== MAIN ==================
def main():
    global bot_app

    # Initialize database
    init_db()

    # Create event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Start HTTP server for Render
    loop.create_task(run_http_server())

    # Create bot application
    bot_app = Application.builder().token(TOKEN).build()

    # Delete any existing webhook to allow polling
    async def delete_webhook():
        try:
            await bot_app.bot.delete_webhook()
            logger.info("✅ Existing webhook deleted (if any)")
        except Exception as e:
            logger.error(f"Failed to delete webhook: {e}")

    # Run webhook deletion before starting polling
    loop.run_until_complete(delete_webhook())

    # Conversation handler for /setrepeat
    setrepeat_conv = ConversationHandler(
        entry_points=[CommandHandler('setrepeat', setrepeat_start)],
        states={
            INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setrepeat_interval)],
            EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, setrepeat_expiry)],
            CONTENT: [MessageHandler(filters.ALL & ~filters.COMMAND, setrepeat_content)],
            TARGETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setrepeat_targets)],
            AUTO_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, setrepeat_autodelete)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    bot_app.add_handler(setrepeat_conv)

    # Conversation handler for /addrule
    addrule_conv = ConversationHandler(
        entry_points=[CommandHandler('addrule', addrule_start)],
        states={
            RULE_TRIGGER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrule_trigger_type)],
            RULE_PATTERN: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrule_pattern)],
            RULE_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrule_reply)],
            RULE_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrule_options)],
        },
        fallbacks=[CommandHandler('cancel', addrule_cancel)],
    )
    bot_app.add_handler(addrule_conv)

    # Basic commands
    bot_app.add_handler(CommandHandler('start', start))
    bot_app.add_handler(CommandHandler('help', help_command))
    bot_app.add_handler(CommandHandler('stopjob', stop_job))
    bot_app.add_handler(CommandHandler('myjobs', my_jobs))
    bot_app.add_handler(CommandHandler('stats', stats))
    bot_app.add_handler(CommandHandler('cancel', cancel))
    bot_app.add_handler(CommandHandler('test', test_command))  # test command

    # Owner commands
    bot_app.add_handler(CommandHandler('backup', backup_command))
    bot_app.add_handler(CommandHandler('restore', restore_command))
    bot_app.add_handler(CommandHandler('blacklist', blacklist_command))
    bot_app.add_handler(CommandHandler('ghostenable', ghost_enable))
    bot_app.add_handler(CommandHandler('ghostdisable', ghost_disable))
    bot_app.add_handler(CommandHandler('sudo', sudo_command))
    bot_app.add_handler(CommandHandler('kick', kick_member))
    bot_app.add_handler(CommandHandler('ban', ban_member))
    bot_app.add_handler(CommandHandler('unban', unban_member))
    bot_app.add_handler(CommandHandler('setwelcome', setwelcome))
    bot_app.add_handler(CommandHandler('setgoodbye', setgoodbye))
    bot_app.add_handler(CommandHandler('addpremium', add_premium_command))
    bot_app.add_handler(CommandHandler('removepremium', remove_premium_command))
    bot_app.add_handler(CommandHandler('premiumlist', premium_list_command))
    bot_app.add_handler(CommandHandler('rules', rules_list))
    bot_app.add_handler(CommandHandler('deleterule', deleterule_command))

    # Chat member handler for welcome/goodbye
    bot_app.add_handler(ChatMemberHandler(track_chat_members, ChatMemberHandler.CHAT_MEMBER))

    # Message handler for tracking and rules
    bot_app.add_handler(MessageHandler(filters.ALL, track_chats))

    # Start scheduler
    scheduler.start()
    load_jobs_from_db_into_scheduler()

    # Schedule auto backup (daily at midnight)
    scheduler.add_job(auto_backup_job, 'cron', hour=0, minute=0, id='auto_backup', replace_existing=True)

    # Start bot
    logger.info("Bot started successfully! Starting polling...")
    loop.run_until_complete(bot_app.run_polling())

if __name__ == "__main__":
    main()
