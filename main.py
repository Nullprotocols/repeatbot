# main.py
# Telegram Repeat Bot – Ultimate Edition
# Includes: Repeat Jobs, Premium, Blacklist, Ghost Mode, Sudo, Moderation,
#           Welcome/Goodbye (with media), Auto-Reply Rules, Help, Banned Words,
#           and all commands are now conversation-based.
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
from typing import Optional, Dict, List, Any
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

# New conversation states for various commands
WELCOME_MSG, WELCOME_MEDIA = range(200, 202)
GOODBYE_MSG, GOODBYE_MEDIA = range(203, 205)
PREMIUM_USER_ID, PREMIUM_DURATION = range(206, 208)
REMOVE_PREMIUM_USER_ID = 209
BANNED_WORD_INPUT = 210
KICK_USER, KICK_REASON = range(211, 213)
BAN_USER, BAN_REASON = range(214, 216)
UNBAN_USER = 217
BLACKLIST_ACTION, BLACKLIST_ID, BLACKLIST_REASON = range(218, 221)

# Additional state for remote group selection
ASK_GROUP_ID = 300

# Global bot app reference (for scheduler callbacks)
bot_app = None

# ================== LOGGING ==================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
        return value
    elif unit == 'm':
        return value * 60
    elif unit == 'h':
        return value * 3600
    elif unit == 'd':
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

# ================== HELPER: GROUP ADMIN CHECK (by group_id) ==================
async def is_user_admin_in_group(user_id: int, group_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if a user is admin in a specific group (or owner)."""
    if user_id == OWNER_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=group_id, user_id=user_id)
        return member.status in ['administrator', 'creator']
    except Exception as e:
        logger.error(f"Failed to check admin status for user {user_id} in group {group_id}: {e}")
        return False

# ================== HELPER: ASK FOR GROUP ID IN PRIVATE ==================
async def ask_for_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state: int):
    """Ask user to provide group ID when command is used in private chat."""
    await update.message.reply_text(
        "🔍 *Aapne ye command private mein use ki hai.*\n\n"
        "Kripya us group ka **ID** bhejein jisme aap ye setting apply karna chahte hain.\n"
        "Agar aapko group ID nahi pata to bot ko group mein add karke wahan se command use karein.",
        parse_mode='Markdown'
    )
    context.user_data['next_state_after_group'] = next_state
    return ASK_GROUP_ID

async def handle_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the group ID input and verify admin status."""
    text = update.message.text.strip()
    try:
        group_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Galat ID. Sirf numbers allow hain. Phir se koshish karein.")
        return ASK_GROUP_ID

    # Check if user is admin in that group
    user_id = update.effective_user.id
    if not await is_user_admin_in_group(user_id, group_id, context):
        await update.message.reply_text("❌ Aap us group ke admin nahi hain. Isliye ye setting apply nahi kar sakte.")
        return ConversationHandler.END

    # Store group_id in user_data and proceed to original state
    context.user_data['target_group_id'] = group_id
    next_state = context.user_data.pop('next_state_after_group')
    # Now we need to call the appropriate function based on next_state
    # This is a bit tricky; we'll use a mapping or we can set a flag and then in the next step we use the stored group_id.
    # For simplicity, we'll just return the next_state and the handler for that state will use the stored group_id.
    return next_state

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
        delete_sent_message_from_db(message_id, chat_id)
    except Exception as e:
        logger.error(f"Failed to delete message {message_id} in chat {chat_id}: {e}")

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
        "Example: `30s`, `10m`, `2h`, `1d`\n"
        "Ya sidhe seconds mein number: `60`, `3600`\n\n"
        "*Note:* Seconds 1 se zyada, minutes 1 se zyada, hours 1 se zyada, days 1 se zyada ho sakte hain.",
        parse_mode='Markdown'
    )
    return INTERVAL

async def setrepeat_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    seconds = parse_time_to_seconds(text, allow_zero=False)
    if seconds is None:
        await update.message.reply_text("❌ *Galat format.*", parse_mode='Markdown')
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
        await update.message.reply_text("❌ *Galat format.*", parse_mode='Markdown')
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
            await update.message.reply_text("❌ *Galat format.*", parse_mode='Markdown')
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
    # Determine target group
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, RULE_TRIGGER_TYPE)
    else:
        # In group, use current chat
        context.user_data['target_group_id'] = update.effective_chat.id
        return await addrule_after_group(update, context)

async def addrule_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is called after group is determined (either from private input or directly in group)
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    # Check admin
    user_id = update.effective_user.id
    if not await is_user_admin_in_group(user_id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
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
        # Validate regex
        try:
            re.compile(pattern)
        except re.error:
            await update.message.reply_text("❌ Invalid regex pattern. Please check and try again.")
            return RULE_PATTERN

    context.user_data['rule_pattern'] = pattern
    context.user_data['rule_is_regex'] = is_regex

    await update.message.reply_text(
        "*💬 Reply bhejo*\n\n"
        "Trigger match hone par bot kya reply karega?\n"
        "Aap **text** bhej sakte ho, ya **media** (photo, video, sticker, etc.).\n"
        "Jo bhi bhejoge, wahi reply hoga.\n"
        "Text mein `{mention}` use kar sakte ho.\n\n"
        "Ab apna reply bhejo:",
        parse_mode='Markdown'
    )
    return RULE_REPLY

async def addrule_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        await update.effective_message.reply_text("❌ Kuch gadbad hui.")
        return ConversationHandler.END

    # Detect media type
    if message.text:
        context.user_data['reply_media_type'] = 'text'
        context.user_data['reply_text'] = message.text
    elif message.photo:
        context.user_data['reply_media_type'] = 'photo'
        context.user_data['reply_media_file_id'] = message.photo[-1].file_id
        context.user_data['reply_caption'] = message.caption
    elif message.video:
        context.user_data['reply_media_type'] = 'video'
        context.user_data['reply_media_file_id'] = message.video.file_id
        context.user_data['reply_caption'] = message.caption
    elif message.document:
        context.user_data['reply_media_type'] = 'document'
        context.user_data['reply_media_file_id'] = message.document.file_id
        context.user_data['reply_caption'] = message.caption
    elif message.audio:
        context.user_data['reply_media_type'] = 'audio'
        context.user_data['reply_media_file_id'] = message.audio.file_id
        context.user_data['reply_caption'] = message.caption
    elif message.voice:
        context.user_data['reply_media_type'] = 'voice'
        context.user_data['reply_media_file_id'] = message.voice.file_id
        context.user_data['reply_caption'] = message.caption
    elif message.video_note:
        context.user_data['reply_media_type'] = 'video_note'
        context.user_data['reply_media_file_id'] = message.video_note.file_id
    elif message.sticker:
        context.user_data['reply_media_type'] = 'sticker'
        context.user_data['reply_media_file_id'] = message.sticker.file_id
    else:
        await message.reply_text("❌ Unsupported media type for reply. Please send text, photo, video, document, voice, sticker, etc.")
        return RULE_REPLY

    # Proceed to options
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
        group_id = context.user_data.get('target_group_id')
        trigger_type = context.user_data['rule_trigger_type']
        pattern = context.user_data['rule_pattern']
        is_regex = context.user_data['rule_is_regex']
        reply_media_type = context.user_data.get('reply_media_type')
        reply_media_file_id = context.user_data.get('reply_media_file_id')
        reply_caption = context.user_data.get('reply_caption')
        reply_text = context.user_data.get('reply_text')
        opts = context.user_data['rule_options']

        if reply_media_type == 'text':
            reply_template = reply_text
        else:
            reply_template = None

        rule_id = add_rule(
            group_id=group_id,
            trigger_type=trigger_type,
            trigger_pattern=pattern,
            is_regex=is_regex,
            reply_template=reply_template,
            auto_delete_trigger=opts.get('auto_delete_trigger', False),
            auto_delete_reply=opts.get('auto_delete_reply', False),
            auto_delete_seconds=opts.get('auto_delete_seconds', 0),
            warn_on_trigger=opts.get('warn_on_trigger', False),
            warn_count=opts.get('warn_count', 0),
            notify_user=opts.get('notify_user', False),
            exempt_admins=opts.get('exempt_admins', True),
            created_by=update.effective_user.id,
            reply_media_type=reply_media_type,
            reply_media_file_id=reply_media_file_id,
            reply_caption=reply_caption
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
    # Determine target group
    if update.effective_chat.type == 'private':
        if context.args:
            try:
                group_id = int(context.args[0])
            except:
                await update.message.reply_text("❌ Galat group ID. Usage: /rules <group_id>")
                return
        else:
            await update.message.reply_text("❌ Private mein group ID dena hoga. Usage: /rules <group_id>")
            return
    else:
        group_id = update.effective_chat.id

    # Check admin (optional, since rules are visible to all? but we can check)
    user_id = update.effective_user.id
    if not await is_user_admin_in_group(user_id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return

    rules = get_rules(group_id)
    if not rules:
        await update.message.reply_text("📭 Is group mein koi rule nahi hai.")
        return

    msg = f"*📋 Group Rules ({len(rules)}):*\n\n"
    for rule in rules:
        trigger = rule['trigger_type']
        if rule['trigger_pattern']:
            trigger += f" ({rule['trigger_pattern']})"
        msg += f"*{rule['rule_id']}.* Trigger: `{trigger}`\n"
        if rule['reply_media_type'] and rule['reply_media_type'] != 'text':
            reply_info = f"Media reply ({rule['reply_media_type']})"
        else:
            reply_info = f"Text reply: {rule['reply_template'] or 'None'}"
        msg += f"   Reply: {reply_info}\n"
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
    # Determine target group
    if update.effective_chat.type == 'private':
        if len(context.args) < 2:
            await update.message.reply_text("Usage in private: /deleterule <group_id> <rule_id>")
            return
        try:
            group_id = int(context.args[0])
            rule_id = int(context.args[1])
        except:
            await update.message.reply_text("❌ Galat format. Usage: /deleterule <group_id> <rule_id>")
            return
    else:
        if len(context.args) < 1:
            await update.message.reply_text("Usage: /deleterule <rule_id>")
            return
        group_id = update.effective_chat.id
        try:
            rule_id = int(context.args[0])
        except:
            await update.message.reply_text("❌ Galat rule ID.")
            return

    # Check admin
    user_id = update.effective_user.id
    if not await is_user_admin_in_group(user_id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return

    rule = get_rule(rule_id)
    if not rule or rule['group_id'] != group_id:
        await update.message.reply_text("❌ Rule ID invalid ya is group ka nahi hai.")
        return

    if delete_rule(rule_id):
        await update.message.reply_text(f"✅ Rule `{rule_id}` delete kar diya gaya.", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ Delete failed.")

# ================== SEND RULE REPLY (MEDIA SUPPORT) ==================
async def send_rule_reply(rule, message, context):
    chat_id = message.chat_id
    reply_to = message.message_id
    user = message.from_user
    mention = user.mention_html() if user else ""
    try:
        media_type = rule.get('reply_media_type')
        if media_type == 'text' or (media_type is None and rule.get('reply_template')):
            text = rule['reply_template']
            if text and '{mention}' in text:
                text = text.replace('{mention}', mention)
            return await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to, parse_mode='HTML')
        elif media_type == 'photo':
            caption = rule.get('reply_caption', '')
            if caption and '{mention}' in caption:
                caption = caption.replace('{mention}', mention)
            return await context.bot.send_photo(chat_id=chat_id, photo=rule['reply_media_file_id'], caption=caption, reply_to_message_id=reply_to, parse_mode='HTML')
        elif media_type == 'video':
            caption = rule.get('reply_caption', '')
            if caption and '{mention}' in caption:
                caption = caption.replace('{mention}', mention)
            return await context.bot.send_video(chat_id=chat_id, video=rule['reply_media_file_id'], caption=caption, reply_to_message_id=reply_to, parse_mode='HTML')
        elif media_type == 'document':
            caption = rule.get('reply_caption', '')
            if caption and '{mention}' in caption:
                caption = caption.replace('{mention}', mention)
            return await context.bot.send_document(chat_id=chat_id, document=rule['reply_media_file_id'], caption=caption, reply_to_message_id=reply_to, parse_mode='HTML')
        elif media_type == 'audio':
            caption = rule.get('reply_caption', '')
            if caption and '{mention}' in caption:
                caption = caption.replace('{mention}', mention)
            return await context.bot.send_audio(chat_id=chat_id, audio=rule['reply_media_file_id'], caption=caption, reply_to_message_id=reply_to, parse_mode='HTML')
        elif media_type == 'voice':
            caption = rule.get('reply_caption', '')
            if caption and '{mention}' in caption:
                caption = caption.replace('{mention}', mention)
            return await context.bot.send_voice(chat_id=chat_id, voice=rule['reply_media_file_id'], caption=caption, reply_to_message_id=reply_to, parse_mode='HTML')
        elif media_type == 'video_note':
            return await context.bot.send_video_note(chat_id=chat_id, video_note=rule['reply_media_file_id'], reply_to_message_id=reply_to)
        elif media_type == 'sticker':
            return await context.bot.send_sticker(chat_id=chat_id, sticker=rule['reply_media_file_id'], reply_to_message_id=reply_to)
    except Exception as e:
        logger.error(f"Failed to send rule reply: {e}")
        return None

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
        content = message.sticker.emoji
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
        reply_msg = await send_rule_reply(rule, message, context)
        if reply_msg and rule['auto_delete_reply'] and rule['auto_delete_seconds'] > 0:
            delete_at = datetime.now() + timedelta(seconds=rule['auto_delete_seconds'])
            save_sent_message(f"rule_{rule['rule_id']}", chat.id, reply_msg.message_id, delete_at, rule['rule_id'])
            scheduler.add_job(
                delete_message,
                DateTrigger(run_date=delete_at),
                args=[f"rule_{rule['rule_id']}", chat.id, reply_msg.message_id],
                id=f"del_rule_{rule['rule_id']}_{reply_msg.message_id}",
                replace_existing=True
            )

        # 3. Warn user
        if rule['warn_on_trigger'] and rule['warn_count'] > 0:
            add_warn(user.id, chat.id, rule['rule_id'], update.effective_user.id, f"Rule {rule['rule_id']} triggered")

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

        break

# ================== BANNED WORDS CHECKER ==================
async def check_banned_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        return
    message = update.message
    if not message:
        return
    user = update.effective_user
    if not user:
        return
    # Exempt owner and admins
    if user.id == OWNER_ID:
        return
    try:
        member = await chat.get_member(user.id)
        if member.status in ['administrator', 'creator']:
            return
    except:
        pass

    banned = get_banned_words(chat.id)
    if not banned:
        return
    text = message.text or message.caption
    if not text:
        return
    text_lower = text.lower()
    for word in banned:
        if word.lower() in text_lower:
            try:
                await message.delete()
                logger.info(f"Deleted message containing banned word '{word}' from {user.id} in {chat.id}")
                break
            except Exception as e:
                logger.error(f"Failed to delete banned message: {e}")
            break

# ================== WELCOME/GOODBYE MEDIA SENDER ==================
async def send_welcome_or_goodbye(chat_id: int, user, context, is_welcome: bool):
    """Send welcome or goodbye message with optional media."""
    settings = get_group_settings(chat_id)
    
    if is_welcome:
        enabled = settings.get('welcome_enabled', False)
        media_type = settings.get('welcome_media_type')
        file_id = settings.get('welcome_media_file_id')
        caption = settings.get('welcome_caption', '')
        text = settings.get('welcome_message', '')
    else:
        enabled = settings.get('goodbye_enabled', False)
        media_type = settings.get('goodbye_media_type')
        file_id = settings.get('goodbye_media_file_id')
        caption = settings.get('goodbye_caption', '')
        text = settings.get('goodbye_message', '')
    
    if not enabled:
        return
    
    name = user.mention_html() if user.username else user.full_name
    
    # Replace {name} in text and caption
    if text:
        text = text.replace('{name}', name)
    if caption:
        caption = caption.replace('{name}', name)
    
    try:
        if media_type and file_id:
            if media_type == 'photo':
                await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption or text, parse_mode='HTML')
            elif media_type == 'video':
                await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption or text, parse_mode='HTML')
            elif media_type == 'document':
                await context.bot.send_document(chat_id=chat_id, document=file_id, caption=caption or text, parse_mode='HTML')
            elif media_type == 'audio':
                await context.bot.send_audio(chat_id=chat_id, audio=file_id, caption=caption or text, parse_mode='HTML')
            elif media_type == 'voice':
                await context.bot.send_voice(chat_id=chat_id, voice=file_id, caption=caption or text, parse_mode='HTML')
            elif media_type == 'video_note':
                await context.bot.send_video_note(chat_id=chat_id, video_note=file_id)
            elif media_type == 'sticker':
                await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text or "Welcome!", parse_mode='HTML')
        else:
            if text:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Failed to send welcome/goodbye: {e}")

# ================== CONVERSATION: /setwelcome (with media) ==================
async def setwelcome_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Determine target group
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, WELCOME_MSG)
    else:
        context.user_data['target_group_id'] = update.effective_chat.id
        return await setwelcome_after_group(update, context)

async def setwelcome_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 *Welcome Message Set Karein*\n\n"
        "Ab aap jo **text message** bhejenge wo welcome message ban jayega.\n"
        "Aap `{name}` use kar sakte ho.\n\n"
        "Agar aap **media** (photo, video, sticker) bhejna chahte ho to 'media' likhein.\n"
        "Ya 'off' likh kar disable karein.",
        parse_mode='Markdown'
    )
    return WELCOME_MSG

async def setwelcome_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    group_id = context.user_data.get('target_group_id')
    if text == 'media':
        await update.message.reply_text(
            "📸 *Media Welcome*\n\n"
            "Ab jo media aap bhejenge (photo, video, sticker, etc.) wo welcome message ban jayega.\n"
            "Caption mein bhi `{name}` use kar sakte ho.",
            parse_mode='Markdown'
        )
        return WELCOME_MEDIA
    elif text == 'off':
        set_group_welcome(group_id, "", False)
        await update.message.reply_text("✅ Welcome message disabled.")
        context.user_data.clear()
        return ConversationHandler.END
    else:
        set_group_welcome(group_id, update.message.text, True)
        await update.message.reply_text(f"✅ Welcome text message set ho gaya!")
        context.user_data.clear()
        return ConversationHandler.END

async def setwelcome_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    group_id = context.user_data.get('target_group_id')
    
    media_type = None
    file_id = None
    caption = message.caption or ""
    
    if message.photo:
        media_type = 'photo'
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = 'video'
        file_id = message.video.file_id
    elif message.document:
        media_type = 'document'
        file_id = message.document.file_id
    elif message.audio:
        media_type = 'audio'
        file_id = message.audio.file_id
    elif message.voice:
        media_type = 'voice'
        file_id = message.voice.file_id
    elif message.video_note:
        media_type = 'video_note'
        file_id = message.video_note.file_id
    elif message.sticker:
        media_type = 'sticker'
        file_id = message.sticker.file_id
    else:
        await update.message.reply_text("❌ Unsupported media type. Please send photo, video, document, etc.")
        return WELCOME_MEDIA
    
    # Save to database
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO group_settings (group_id, welcome_enabled, welcome_media_type, welcome_media_file_id, welcome_caption, updated_at)
        VALUES (?, 1, ?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            welcome_enabled = 1,
            welcome_media_type = ?,
            welcome_media_file_id = ?,
            welcome_caption = ?,
            updated_at = ?
    ''', (group_id, media_type, file_id, caption, now, media_type, file_id, caption, now))
    conn.commit()
    conn.close()
    
    await update.message.reply_text("✅ Media welcome message set ho gaya!")
    context.user_data.clear()
    return ConversationHandler.END

async def setwelcome_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /setgoodbye (with media) ==================
async def setgoodbye_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, GOODBYE_MSG)
    else:
        context.user_data['target_group_id'] = update.effective_chat.id
        return await setgoodbye_after_group(update, context)

async def setgoodbye_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📝 *Goodbye Message Set Karein*\n\n"
        "Ab aap jo **text message** bhejenge wo goodbye message ban jayega.\n"
        "Aap `{name}` use kar sakte ho.\n\n"
        "Agar aap **media** bhejna chahte ho to 'media' likhein.\n"
        "Ya 'off' likh kar disable karein.",
        parse_mode='Markdown'
    )
    return GOODBYE_MSG

async def setgoodbye_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    group_id = context.user_data.get('target_group_id')
    if text == 'media':
        await update.message.reply_text(
            "📸 *Media Goodbye*\n\n"
            "Ab jo media aap bhejenge wo goodbye message ban jayega.\n"
            "Caption mein `{name}` use kar sakte ho.",
            parse_mode='Markdown'
        )
        return GOODBYE_MEDIA
    elif text == 'off':
        set_group_goodbye(group_id, "", False)
        await update.message.reply_text("✅ Goodbye message disabled.")
        context.user_data.clear()
        return ConversationHandler.END
    else:
        set_group_goodbye(group_id, update.message.text, True)
        await update.message.reply_text(f"✅ Goodbye text message set ho gaya!")
        context.user_data.clear()
        return ConversationHandler.END

async def setgoodbye_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    group_id = context.user_data.get('target_group_id')
    
    media_type = None
    file_id = None
    caption = message.caption or ""
    
    if message.photo:
        media_type = 'photo'
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = 'video'
        file_id = message.video.file_id
    elif message.document:
        media_type = 'document'
        file_id = message.document.file_id
    elif message.audio:
        media_type = 'audio'
        file_id = message.audio.file_id
    elif message.voice:
        media_type = 'voice'
        file_id = message.voice.file_id
    elif message.video_note:
        media_type = 'video_note'
        file_id = message.video_note.file_id
    elif message.sticker:
        media_type = 'sticker'
        file_id = message.sticker.file_id
    else:
        await update.message.reply_text("❌ Unsupported media type. Please send photo, video, document, etc.")
        return GOODBYE_MEDIA
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO group_settings (group_id, goodbye_enabled, goodbye_media_type, goodbye_media_file_id, goodbye_caption, updated_at)
        VALUES (?, 1, ?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            goodbye_enabled = 1,
            goodbye_media_type = ?,
            goodbye_media_file_id = ?,
            goodbye_caption = ?,
            updated_at = ?
    ''', (group_id, media_type, file_id, caption, now, media_type, file_id, caption, now))
    conn.commit()
    conn.close()
    
    await update.message.reply_text("✅ Media goodbye message set ho gaya!")
    context.user_data.clear()
    return ConversationHandler.END

async def setgoodbye_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /addbannedword ==================
async def addbannedword_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, BANNED_WORD_INPUT)
    else:
        context.user_data['target_group_id'] = update.effective_chat.id
        return await addbannedword_after_group(update, context)

async def addbannedword_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🚫 *Banned Word Add Karein*\n\n"
        "Ek ya multiple words likho (comma ya space se separate kar sakte ho).\n"
        "Example: `hello world` ya `hello, world`",
        parse_mode='Markdown'
    )
    return BANNED_WORD_INPUT

async def addbannedword_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = update.message.text.strip()
    group_id = context.user_data.get('target_group_id')
    if ',' in full_text:
        words = [w.strip() for w in full_text.split(',') if w.strip()]
    else:
        words = full_text.split()
    
    if not words:
        await update.message.reply_text("❌ Koi word nahi mila.")
        return ConversationHandler.END
    
    added = 0
    for w in words:
        add_banned_word(group_id, w, update.effective_user.id)
        added += 1
    
    await update.message.reply_text(f"✅ {added} banned word(s) add ho gaye.")
    context.user_data.clear()
    return ConversationHandler.END

async def addbannedword_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /removebannedword ==================
async def removebannedword_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, BANNED_WORD_INPUT)
    else:
        context.user_data['target_group_id'] = update.effective_chat.id
        return await removebannedword_after_group(update, context)

async def removebannedword_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🗑️ *Banned Word Remove Karein*\n\n"
        "Ek ya multiple words likho jo remove karne hain.",
        parse_mode='Markdown'
    )
    return BANNED_WORD_INPUT

async def removebannedword_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = update.message.text.strip()
    group_id = context.user_data.get('target_group_id')
    if ',' in full_text:
        words = [w.strip() for w in full_text.split(',') if w.strip()]
    else:
        words = full_text.split()
    
    if not words:
        await update.message.reply_text("❌ Koi word nahi mila.")
        return ConversationHandler.END
    
    removed = 0
    not_found = 0
    for w in words:
        if remove_banned_word(group_id, w):
            removed += 1
        else:
            not_found += 1
    
    msg = f"✅ {removed} word(s) remove ho gaye."
    if not_found:
        msg += f"\n⚠️ {not_found} word(s) list mein nahi the."
    await update.message.reply_text(msg)
    context.user_data.clear()
    return ConversationHandler.END

async def removebannedword_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /kick ==================
async def kick_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, KICK_USER)
    else:
        context.user_data['target_group_id'] = update.effective_chat.id
        return await kick_after_group(update, context)

async def kick_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return ConversationHandler.END

    await update.message.reply_text(
        "👢 *Kick User*\n\n"
        "Jis user ko group se nikalna hai, uska **username ya ID** do:",
        parse_mode='Markdown'
    )
    return KICK_USER

async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    identifier = update.message.text.strip()
    context.user_data['kick_target'] = identifier
    await update.message.reply_text(
        "📝 *Reason (optional)*\n\n"
        "Agar koi reason dena hai to likho, warna 'skip' likho.",
        parse_mode='Markdown'
    )
    return KICK_REASON

async def kick_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    if reason.lower() == 'skip':
        reason = "No reason"
    
    identifier = context.user_data['kick_target']
    group_id = context.user_data.get('target_group_id')
    try:
        if identifier.startswith('@'):
            user = await context.bot.get_chat(identifier)
            user_id = user.id
            mention = user.mention_html()
        else:
            user_id = int(identifier)
            try:
                user = await context.bot.get_chat(user_id)
                mention = user.mention_html()
            except:
                mention = f"<code>{user_id}</code>"
        
        await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)  # kick
        await context.bot.send_message(
            OWNER_ID,
            f"👢 *Kick Action*\nGroup: `{group_id}`\nUser: {mention}\nReason: {reason}\nBy: {update.effective_user.mention_html()}",
            parse_mode='HTML'
        )
        await update.message.reply_text(f"✅ {mention} ko group se nikal diya gaya.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    context.user_data.clear()
    return ConversationHandler.END

async def kick_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /ban ==================
async def ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, BAN_USER)
    else:
        context.user_data['target_group_id'] = update.effective_chat.id
        return await ban_after_group(update, context)

async def ban_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🔨 *Ban User*\n\n"
        "Jis user ko permanently ban karna hai, uska **username ya ID** do:",
        parse_mode='Markdown'
    )
    return BAN_USER

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    identifier = update.message.text.strip()
    context.user_data['ban_target'] = identifier
    await update.message.reply_text(
        "📝 *Reason (optional)*\n\n"
        "Reason likho ya 'skip':",
        parse_mode='Markdown'
    )
    return BAN_REASON

async def ban_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    if reason.lower() == 'skip':
        reason = "No reason"
    
    identifier = context.user_data['ban_target']
    group_id = context.user_data.get('target_group_id')
    try:
        if identifier.startswith('@'):
            user = await context.bot.get_chat(identifier)
            user_id = user.id
            mention = user.mention_html()
        else:
            user_id = int(identifier)
            try:
                user = await context.bot.get_chat(user_id)
                mention = user.mention_html()
            except:
                mention = f"<code>{user_id}</code>"
        
        await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.send_message(
            OWNER_ID,
            f"🔨 *Ban Action*\nGroup: `{group_id}`\nUser: {mention}\nReason: {reason}\nBy: {update.effective_user.mention_html()}",
            parse_mode='HTML'
        )
        await update.message.reply_text(f"✅ {mention} ko permanently ban kar diya gaya.", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    context.user_data.clear()
    return ConversationHandler.END

async def ban_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /unban ==================
async def unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        return await ask_for_group_id(update, context, UNBAN_USER)
    else:
        context.user_data['target_group_id'] = update.effective_chat.id
        return await unban_after_group(update, context)

async def unban_after_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get('target_group_id')
    if not group_id:
        await update.message.reply_text("❌ Kuch gadbad hui. Phir se try karein.")
        return ConversationHandler.END

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🔓 *Unban User*\n\n"
        "Jis user ka ban hatana hai, uska **username ya ID** do:",
        parse_mode='Markdown'
    )
    return UNBAN_USER

async def unban_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    identifier = update.message.text.strip()
    group_id = context.user_data.get('target_group_id')
    try:
        if identifier.startswith('@'):
            user = await context.bot.get_chat(identifier)
            user_id = user.id
        else:
            user_id = int(identifier)
        
        await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        await update.message.reply_text(f"✅ User {identifier} ka ban hata diya gaya.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    context.user_data.clear()
    return ConversationHandler.END

async def unban_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /blacklist (owner only) ==================
async def blacklist_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "🚫 *Blacklist Management*\n\n"
        "Kya karna chahte ho?\n"
        "• `add` - naya entry add karo\n"
        "• `remove` - entry hatao\n"
        "• `list` - saari blacklisted IDs dekho\n\n"
        "Ek option likho:",
        parse_mode='Markdown'
    )
    return BLACKLIST_ACTION

async def blacklist_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = update.message.text.strip().lower()
    if action == 'list':
        blacklisted = get_all_blacklisted()
        if not blacklisted:
            await update.message.reply_text("📭 Blacklist empty hai.")
        else:
            msg = "*🚫 Blacklisted IDs:*\n\n"
            for item in blacklisted:
                msg += f"• `{item['target_id']}` ({item['target_type']}) - {item['reason']}\n"
            await update.message.reply_text(msg, parse_mode='Markdown')
        return ConversationHandler.END
    elif action == 'add':
        await update.message.reply_text(
            "➕ *Add to Blacklist*\n\n"
            "Target ID do (user ya chat ID):",
            parse_mode='Markdown'
        )
        context.user_data['blacklist_action'] = 'add'
        return BLACKLIST_ID
    elif action == 'remove':
        await update.message.reply_text(
            "➖ *Remove from Blacklist*\n\n"
            "Target ID do:",
            parse_mode='Markdown'
        )
        context.user_data['blacklist_action'] = 'remove'
        return BLACKLIST_ID
    else:
        await update.message.reply_text("❌ Galat option. 'add', 'remove', ya 'list' likho.")
        return BLACKLIST_ACTION

async def blacklist_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text.strip())
        context.user_data['blacklist_target'] = target_id
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. Sirf numbers allowed hain.")
        return BLACKLIST_ID
    
    if context.user_data['blacklist_action'] == 'add':
        await update.message.reply_text("📝 *Reason (optional)*\n\nReason likho ya 'skip':", parse_mode='Markdown')
        return BLACKLIST_REASON
    else:  # remove
        if remove_from_blacklist(target_id):
            await update.message.reply_text(f"✅ `{target_id}` blacklist se hata diya.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ `{target_id}` blacklist mein nahi mila.", parse_mode='Markdown')
        context.user_data.clear()
        return ConversationHandler.END

async def blacklist_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    if reason.lower() == 'skip':
        reason = "No reason"
    
    target_id = context.user_data['blacklist_target']
    add_to_blacklist(target_id, 'user', reason, update.effective_user.id)
    await update.message.reply_text(f"✅ `{target_id}` blacklist mein add ho gaya.\nReason: {reason}", parse_mode='Markdown')
    context.user_data.clear()
    return ConversationHandler.END

async def blacklist_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /addpremium ==================
async def addpremium_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "👑 *Premium User Add Karein*\n\n"
        "Sabse pehle *user ID* batao:",
        parse_mode='Markdown'
    )
    return PREMIUM_USER_ID

async def addpremium_userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
        context.user_data['premium_user_id'] = user_id
        await update.message.reply_text(
            "⏳ *Duration batao*\n\n"
            "Kitne din ke liye premium dena hai?\n"
            "Example: `30d`, `7d`, `365d`",
            parse_mode='Markdown'
        )
        return PREMIUM_DURATION
    except ValueError:
        await update.message.reply_text("❌ Galat user ID. Sirf numbers allowed hain.")
        return PREMIUM_USER_ID

async def addpremium_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    duration_str = update.message.text.strip()
    delta = parse_expiry_to_timedelta(duration_str)
    if delta is None:
        await update.message.reply_text("❌ Galat duration format. Example: `30d`, `7d`", parse_mode='Markdown')
        return PREMIUM_DURATION
    
    user_id = context.user_data['premium_user_id']
    add_premium(user_id, delta)
    
    await update.message.reply_text(
        f"✅ *Premium added!*\nUser: `{user_id}`\nDuration: {duration_str}",
        parse_mode='Markdown'
    )
    context.user_data.clear()
    return ConversationHandler.END

async def addpremium_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ================== CONVERSATION: /removepremium ==================
async def removepremium_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Sirf owner ke liye")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "👑 *Premium User Remove Karein*\n\n"
        "Jis user ka premium hatana hai, uski *user ID* batao:",
        parse_mode='Markdown'
    )
    return REMOVE_PREMIUM_USER_ID

async def removepremium_userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
        if remove_premium(user_id):
            await update.message.reply_text(f"✅ User `{user_id}` ka premium hata diya gaya.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ User `{user_id}` premium list mein nahi mila.", parse_mode='Markdown')
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Galat user ID. Sirf numbers allowed hain.")
        return REMOVE_PREMIUM_USER_ID

async def removepremium_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Process cancelled.")
    return ConversationHandler.END

# ================== OWNER COMMANDS (hidden) ==================
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

# ================== LIST BANNED WORDS ==================
async def listbannedwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Determine target group
    if update.effective_chat.type == 'private':
        if context.args:
            try:
                group_id = int(context.args[0])
            except:
                await update.message.reply_text("❌ Galat group ID. Usage: /listbannedwords <group_id>")
                return
        else:
            await update.message.reply_text("❌ Private mein group ID dena hoga. Usage: /listbannedwords <group_id>")
            return
    else:
        group_id = update.effective_chat.id

    if not await is_user_admin_in_group(update.effective_user.id, group_id, context):
        await update.message.reply_text("❌ Sirf group admin ye command use kar sakte hain.")
        return

    words = get_banned_words(group_id)
    if not words:
        await update.message.reply_text("📭 Is group mein koi banned word nahi hai.")
        return
    msg = "*🚫 Banned Words:*\n" + "\n".join(f"• `{w}`" for w in words)
    await update.message.reply_text(msg, parse_mode='Markdown')

# ================== BASIC COMMANDS ==================
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
/cancel - Kisi bhi conversation ko cancel karein
/test - Bot alive hai check karein

*👑 Premium User Commands:*
/setrepeat - Nayi repeat job banayein (conversation)
/myjobs - Apni saari jobs dekhein
/stopjob <job_id> - Kisi job ko rok dein
/stats - Bot ke statistics dekhein

*🛡️ Group Admin Commands (Group mein ya private mein group ID dekar):*
/setwelcome - Welcome message set karein (text/media)
/setgoodbye - Goodbye message set karein (text/media)
/addbannedword - Banned word add karein (multiple)
/removebannedword - Banned word hatao
/listbannedwords - Banned words dekhein
/addrule - Auto-reply rule banayein (media reply ke saath)
/rules - Group ke saare rules dekhein
/deleterule <rule_id> - Rule delete karein
/kick - User ko group se nikalo
/ban - User ko permanently ban karo
/unban - User ka ban hatao
"""
    if is_owner:
        help_text += """
*🕵️ Owner Only Commands:*
/backup - Database backup lo
/restore - Backup se restore karo
/blacklist - Blacklist management
/ghostenable - Group mein ghost mode on
/ghostdisable - Ghost mode off
/sudo - Kisi aur user ki taraf se command chalao
/addpremium - User ko premium do
/removepremium - User ka premium hatao
/premiumlist - Saare premium users dekho
"""
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
            await check_banned_words(update, context)

    if user:
        save_user(user.id)

# ================== CHAT MEMBER HANDLER (for welcome/goodbye) ==================
async def track_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not isinstance(update, ChatMemberUpdated):
        return
    chat = update.effective_chat
    if chat.type not in ['group', 'supergroup']:
        return
    
    # New member joined
    if update.new_chat_member.status == ChatMember.MEMBER and update.old_chat_member.status == ChatMember.LEFT:
        user = update.new_chat_member.user
        await send_welcome_or_goodbye(chat.id, user, context, is_welcome=True)
    
    # Member left
    elif update.old_chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.CREATOR] and update.new_chat_member.status == ChatMember.LEFT:
        user = update.old_chat_member.user
        await send_welcome_or_goodbye(chat.id, user, context, is_welcome=False)

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

    # ========== CONVERSATION HANDLERS ==========

    # /setrepeat
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

    # /addrule
    addrule_conv = ConversationHandler(
        entry_points=[CommandHandler('addrule', addrule_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            RULE_TRIGGER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrule_trigger_type)],
            RULE_PATTERN: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrule_pattern)],
            RULE_REPLY: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL | filters.TEXT, addrule_reply)],
            RULE_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, addrule_options)],
        },
        fallbacks=[CommandHandler('cancel', addrule_cancel)],
    )
    bot_app.add_handler(addrule_conv)

    # /setwelcome
    welcome_conv = ConversationHandler(
        entry_points=[CommandHandler('setwelcome', setwelcome_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            WELCOME_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, setwelcome_receive)],
            WELCOME_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL, setwelcome_media)],
        },
        fallbacks=[CommandHandler('cancel', setwelcome_cancel)],
    )
    bot_app.add_handler(welcome_conv)

    # /setgoodbye
    goodbye_conv = ConversationHandler(
        entry_points=[CommandHandler('setgoodbye', setgoodbye_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            GOODBYE_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, setgoodbye_receive)],
            GOODBYE_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL, setgoodbye_media)],
        },
        fallbacks=[CommandHandler('cancel', setgoodbye_cancel)],
    )
    bot_app.add_handler(goodbye_conv)

    # /addbannedword
    addbanned_conv = ConversationHandler(
        entry_points=[CommandHandler('addbannedword', addbannedword_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            BANNED_WORD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, addbannedword_receive)],
        },
        fallbacks=[CommandHandler('cancel', addbannedword_cancel)],
    )
    bot_app.add_handler(addbanned_conv)

    # /removebannedword
    removebanned_conv = ConversationHandler(
        entry_points=[CommandHandler('removebannedword', removebannedword_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            BANNED_WORD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, removebannedword_receive)],
        },
        fallbacks=[CommandHandler('cancel', removebannedword_cancel)],
    )
    bot_app.add_handler(removebanned_conv)

    # /kick
    kick_conv = ConversationHandler(
        entry_points=[CommandHandler('kick', kick_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            KICK_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, kick_user)],
            KICK_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, kick_reason)],
        },
        fallbacks=[CommandHandler('cancel', kick_cancel)],
    )
    bot_app.add_handler(kick_conv)

    # /ban
    ban_conv = ConversationHandler(
        entry_points=[CommandHandler('ban', ban_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            BAN_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_user)],
            BAN_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_reason)],
        },
        fallbacks=[CommandHandler('cancel', ban_cancel)],
    )
    bot_app.add_handler(ban_conv)

    # /unban
    unban_conv = ConversationHandler(
        entry_points=[CommandHandler('unban', unban_start)],
        states={
            ASK_GROUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_id)],
            UNBAN_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, unban_receive)],
        },
        fallbacks=[CommandHandler('cancel', unban_cancel)],
    )
    bot_app.add_handler(unban_conv)

    # /blacklist (owner only)
    blacklist_conv = ConversationHandler(
        entry_points=[CommandHandler('blacklist', blacklist_start)],
        states={
            BLACKLIST_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, blacklist_action)],
            BLACKLIST_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, blacklist_id)],
            BLACKLIST_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, blacklist_reason)],
        },
        fallbacks=[CommandHandler('cancel', blacklist_cancel)],
    )
    bot_app.add_handler(blacklist_conv)

    # /addpremium
    premium_conv = ConversationHandler(
        entry_points=[CommandHandler('addpremium', addpremium_start)],
        states={
            PREMIUM_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, addpremium_userid)],
            PREMIUM_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, addpremium_duration)],
        },
        fallbacks=[CommandHandler('cancel', addpremium_cancel)],
    )
    bot_app.add_handler(premium_conv)

    # /removepremium
    remove_premium_conv = ConversationHandler(
        entry_points=[CommandHandler('removepremium', removepremium_start)],
        states={
            REMOVE_PREMIUM_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, removepremium_userid)],
        },
        fallbacks=[CommandHandler('cancel', removepremium_cancel)],
    )
    bot_app.add_handler(remove_premium_conv)

    # ========== BASIC COMMAND HANDLERS ==========
    bot_app.add_handler(CommandHandler('start', start))
    bot_app.add_handler(CommandHandler('help', help_command))
    bot_app.add_handler(CommandHandler('stopjob', stop_job))
    bot_app.add_handler(CommandHandler('myjobs', my_jobs))
    bot_app.add_handler(CommandHandler('stats', stats))
    bot_app.add_handler(CommandHandler('cancel', cancel))
    bot_app.add_handler(CommandHandler('test', test_command))
    bot_app.add_handler(CommandHandler('rules', rules_list))
    bot_app.add_handler(CommandHandler('deleterule', deleterule_command))
    bot_app.add_handler(CommandHandler('listbannedwords', listbannedwords))

    # Owner commands
    bot_app.add_handler(CommandHandler('backup', backup_command))
    bot_app.add_handler(CommandHandler('restore', restore_command))
    bot_app.add_handler(CommandHandler('ghostenable', ghost_enable))
    bot_app.add_handler(CommandHandler('ghostdisable', ghost_disable))
    bot_app.add_handler(CommandHandler('sudo', sudo_command))
    bot_app.add_handler(CommandHandler('premiumlist', premium_list_command))

    # ========== CHAT MEMBER HANDLER ==========
    bot_app.add_handler(ChatMemberHandler(track_chat_members, ChatMemberHandler.CHAT_MEMBER))

    # ========== MESSAGE HANDLER (for tracking, rules, banned words) ==========
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
