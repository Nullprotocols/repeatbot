# database.py
# Complete database operations for Telegram Repeat Bot – Ultimate Edition
# Includes all features: repeat jobs, premium, blacklist, ghost mode, welcome/goodbye (with media),
# auto-reply rules (with media reply), warns, banned words, backup/restore.
# Python 3.12.3 compatible
# Render Web Service ready – no external dependencies

import sqlite3
import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

DB_FILE = "bot_data.db"

# ================== INITIALIZATION ==================
def init_db():
    """Initialize all database tables – call once at startup"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_premium BOOLEAN DEFAULT 0,
            premium_expiry TIMESTAMP,
            created_at TIMESTAMP NOT NULL,
            last_seen TIMESTAMP
        )
    ''')

    # Repeat jobs table
    c.execute('''
        CREATE TABLE IF NOT EXISTS repeat_jobs (
            job_id TEXT PRIMARY KEY,
            creator_id INTEGER NOT NULL,
            source_chat_id INTEGER NOT NULL,
            target_ids TEXT,
            interval_seconds INTEGER NOT NULL,
            expiry TIMESTAMP NOT NULL,
            auto_delete_seconds INTEGER,
            created_at TIMESTAMP NOT NULL,
            media_type TEXT,
            media_file_id TEXT,
            caption TEXT,
            poll_data TEXT,
            text TEXT,
            message_count INTEGER DEFAULT 0
        )
    ''')

    # Sent messages table (for auto-delete)
    c.execute('''
        CREATE TABLE IF NOT EXISTS sent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            delete_at TIMESTAMP,
            rule_id INTEGER,
            FOREIGN KEY (job_id) REFERENCES repeat_jobs(job_id)
        )
    ''')

    # Stats table
    c.execute('''
        CREATE TABLE IF NOT EXISTS stats (
            stat_name TEXT PRIMARY KEY,
            stat_value INTEGER DEFAULT 0
        )
    ''')

    # Known chats table
    c.execute('''
        CREATE TABLE IF NOT EXISTS known_chats (
            chat_id INTEGER PRIMARY KEY,
            chat_type TEXT,
            last_seen TIMESTAMP
        )
    ''')

    # Blacklist table
    c.execute('''
        CREATE TABLE IF NOT EXISTS blacklist (
            target_id INTEGER PRIMARY KEY,
            target_type TEXT,  -- 'user' or 'chat'
            reason TEXT,
            created_at TIMESTAMP,
            created_by INTEGER
        )
    ''')

    # Group settings (welcome/goodbye/ghost mode)
    c.execute('''
        CREATE TABLE IF NOT EXISTS group_settings (
            group_id INTEGER PRIMARY KEY,
            welcome_enabled BOOLEAN DEFAULT 0,
            welcome_message TEXT,
            goodbye_enabled BOOLEAN DEFAULT 0,
            goodbye_message TEXT,
            ghost_mode BOOLEAN DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
    ''')

    # Add media columns to group_settings if not exist
    c.execute("PRAGMA table_info(group_settings)")
    columns = [col[1] for col in c.fetchall()]
    if 'welcome_media_type' not in columns:
        c.execute("ALTER TABLE group_settings ADD COLUMN welcome_media_type TEXT")
    if 'welcome_media_file_id' not in columns:
        c.execute("ALTER TABLE group_settings ADD COLUMN welcome_media_file_id TEXT")
    if 'welcome_caption' not in columns:
        c.execute("ALTER TABLE group_settings ADD COLUMN welcome_caption TEXT")
    if 'goodbye_media_type' not in columns:
        c.execute("ALTER TABLE group_settings ADD COLUMN goodbye_media_type TEXT")
    if 'goodbye_media_file_id' not in columns:
        c.execute("ALTER TABLE group_settings ADD COLUMN goodbye_media_file_id TEXT")
    if 'goodbye_caption' not in columns:
        c.execute("ALTER TABLE group_settings ADD COLUMN goodbye_caption TEXT")

    # Ghost forward mapping
    c.execute('''
        CREATE TABLE IF NOT EXISTS ghost_forward (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            forward_to INTEGER NOT NULL,  -- owner's ID
            created_at TIMESTAMP
        )
    ''')

    # Auto-reply rules table
    c.execute('''
        CREATE TABLE IF NOT EXISTS group_rules (
            rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            trigger_type TEXT NOT NULL,  -- 'text', 'photo', 'video', 'document', 'poll', 'voice', 'sticker', 'emoji', 'all'
            trigger_pattern TEXT,  -- keyword ya pattern (NULL for type-based only)
            is_regex BOOLEAN DEFAULT 0,
            reply_template TEXT,
            auto_delete_trigger BOOLEAN DEFAULT 0,
            auto_delete_reply BOOLEAN DEFAULT 0,
            auto_delete_seconds INTEGER DEFAULT 0,
            warn_on_trigger BOOLEAN DEFAULT 0,
            warn_count INTEGER DEFAULT 1,
            notify_user BOOLEAN DEFAULT 0,
            exempt_admins BOOLEAN DEFAULT 1,
            created_by INTEGER,
            created_at TIMESTAMP
        )
    ''')

    # Add media reply columns to group_rules if not exist
    c.execute("PRAGMA table_info(group_rules)")
    columns = [col[1] for col in c.fetchall()]
    if 'reply_media_type' not in columns:
        c.execute("ALTER TABLE group_rules ADD COLUMN reply_media_type TEXT")
    if 'reply_media_file_id' not in columns:
        c.execute("ALTER TABLE group_rules ADD COLUMN reply_media_file_id TEXT")
    if 'reply_caption' not in columns:
        c.execute("ALTER TABLE group_rules ADD COLUMN reply_caption TEXT")

    # User warns table
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_warns (
            warn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            rule_id INTEGER,
            warned_by INTEGER,
            warned_at TIMESTAMP,
            reason TEXT
        )
    ''')

    # Banned words table
    c.execute('''
        CREATE TABLE IF NOT EXISTS banned_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP,
            UNIQUE(group_id, word)
        )
    ''')

    # Initialize stats if not present
    c.execute('INSERT OR IGNORE INTO stats (stat_name, stat_value) VALUES (?, ?)',
              ('total_messages_sent', 0))
    c.execute('INSERT OR IGNORE INTO stats (stat_name, stat_value) VALUES (?, ?)',
              ('total_jobs_created', 0))

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully with all tables")


# ================== BACKUP & RESTORE ==================
def create_backup() -> str:
    """Create a timestamped backup of the database file. Returns filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"backup_{timestamp}.db"
    shutil.copy2(DB_FILE, backup_filename)
    logger.info(f"Database backup created: {backup_filename}")
    return backup_filename

def restore_from_backup(backup_path: str) -> bool:
    """Restore database from a backup file. Returns True on success."""
    try:
        # Create a pre-restore backup just in case
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(DB_FILE, f"pre_restore_{timestamp}.db")
        # Restore
        shutil.copy2(backup_path, DB_FILE)
        # Quick verification
        conn = sqlite3.connect(DB_FILE)
        conn.execute("SELECT COUNT(*) FROM users")
        conn.close()
        logger.info(f"Database restored from {backup_path}")
        return True
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        return False

def cleanup_old_backups(keep: int = 7):
    """Delete old backups, keeping only the latest `keep` ones."""
    backups = [f for f in os.listdir('.') if f.startswith('backup_') and f.endswith('.db')]
    backups.sort(reverse=True)  # newest first
    for old_backup in backups[keep:]:
        os.remove(old_backup)
        logger.info(f"Removed old backup: {old_backup}")


# ================== USER FUNCTIONS ==================
def save_user(user_id: int):
    """Insert or update a user in the database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT OR IGNORE INTO users (user_id, created_at, last_seen)
        VALUES (?, ?, ?)
    ''', (user_id, now, now))
    c.execute('''
        UPDATE users SET last_seen = ? WHERE user_id = ?
    ''', (now, user_id))
    conn.commit()
    conn.close()

def is_premium(user_id: int, owner_id: int) -> bool:
    """Check if a user has premium access (owner always premium)."""
    if user_id == owner_id:
        return True
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT is_premium, premium_expiry FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    is_premium_flag, expiry_str = row
    if not is_premium_flag:
        return False
    if expiry_str:
        expiry = datetime.fromisoformat(expiry_str)
        if expiry < datetime.now():
            return False
    return True

def add_premium(user_id: int, duration_td: timedelta) -> bool:
    """Grant premium to a user for a given duration."""
    expiry = datetime.now() + duration_td
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO users (user_id, is_premium, premium_expiry, created_at)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            is_premium = 1,
            premium_expiry = ?
    ''', (user_id, expiry.isoformat(), datetime.now().isoformat(), expiry.isoformat()))
    conn.commit()
    conn.close()
    logger.info(f"Premium added to user {user_id} until {expiry}")
    return True

def remove_premium(user_id: int) -> bool:
    """Remove premium from a user."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE users SET is_premium = 0, premium_expiry = NULL WHERE user_id = ?', (user_id,))
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    if success:
        logger.info(f"Premium removed from user {user_id}")
    return success

def get_all_premium_users() -> List[Dict]:
    """Return list of all premium users with their expiry."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT user_id, premium_expiry FROM users WHERE is_premium = 1')
    rows = c.fetchall()
    conn.close()
    result = []
    for row in rows:
        user = {'user_id': row[0]}
        if row[1]:
            user['expiry'] = datetime.fromisoformat(row[1])
        else:
            user['expiry'] = None
        result.append(user)
    return result


# ================== CHAT FUNCTIONS ==================
def save_chat(chat_id: int, chat_type: str):
    """Insert or update a known chat."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO known_chats (chat_id, chat_type, last_seen)
        VALUES (?, ?, ?)
    ''', (chat_id, chat_type, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_all_known_chats(chat_type: str = None) -> List[int]:
    """Return list of all known chat IDs, optionally filtered by type."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if chat_type:
        c.execute('SELECT chat_id FROM known_chats WHERE chat_type = ?', (chat_type,))
    else:
        c.execute('SELECT chat_id FROM known_chats')
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]


# ================== BLACKLIST FUNCTIONS ==================
def add_to_blacklist(target_id: int, target_type: str, reason: str = "", created_by: int = 0) -> bool:
    """Add a user or chat to blacklist."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('''
            INSERT OR REPLACE INTO blacklist (target_id, target_type, reason, created_at, created_by)
            VALUES (?, ?, ?, ?, ?)
        ''', (target_id, target_type, reason, datetime.now().isoformat(), created_by))
        conn.commit()
        logger.info(f"Added {target_type} {target_id} to blacklist. Reason: {reason}")
        return True
    except Exception as e:
        logger.error(f"Failed to add to blacklist: {e}")
        return False
    finally:
        conn.close()

def remove_from_blacklist(target_id: int) -> bool:
    """Remove a target from blacklist."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM blacklist WHERE target_id = ?', (target_id,))
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    if success:
        logger.info(f"Removed {target_id} from blacklist")
    return success

def is_blacklisted(target_id: int) -> bool:
    """Check if a target is blacklisted."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT 1 FROM blacklist WHERE target_id = ?', (target_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def get_all_blacklisted() -> List[Dict]:
    """Return list of all blacklisted entries."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT target_id, target_type, reason, created_at FROM blacklist')
    rows = c.fetchall()
    conn.close()
    result = []
    for row in rows:
        result.append({
            'target_id': row[0],
            'target_type': row[1],
            'reason': row[2],
            'created_at': datetime.fromisoformat(row[3]) if row[3] else None
        })
    return result


# ================== JOB FUNCTIONS ==================
def save_job_to_db(job_id: str, creator_id: int, source_chat_id: int, target_ids: List[int],
                   interval_seconds: int, expiry: datetime, auto_delete_seconds: Optional[int],
                   media_type: str = None, media_file_id: str = None, caption: str = None,
                   poll_data: dict = None, text: str = None):
    """Save a repeat job to database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    target_ids_str = ','.join(str(id) for id in target_ids) if target_ids else None
    c.execute('''
        INSERT INTO repeat_jobs (
            job_id, creator_id, source_chat_id, target_ids, interval_seconds,
            expiry, auto_delete_seconds, created_at, media_type, media_file_id, caption,
            poll_data, text, message_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        job_id, creator_id, source_chat_id, target_ids_str, interval_seconds,
        expiry.isoformat(), auto_delete_seconds, datetime.now().isoformat(),
        media_type, media_file_id, caption,
        json.dumps(poll_data) if poll_data else None, text, 0
    ))
    conn.commit()
    conn.close()
    increment_stat('total_jobs_created')

def get_job_from_db(job_id: str) -> Optional[Dict]:
    """Retrieve a job by its ID."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM repeat_jobs WHERE job_id = ?', (job_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    columns = ['job_id', 'creator_id', 'source_chat_id', 'target_ids', 'interval_seconds',
               'expiry', 'auto_delete_seconds', 'created_at', 'media_type', 'media_file_id', 'caption',
               'poll_data', 'text', 'message_count']
    job = dict(zip(columns, row))
    if job['poll_data']:
        job['poll_data'] = json.loads(job['poll_data'])
    if job['target_ids']:
        job['target_ids'] = [int(id) for id in job['target_ids'].split(',')]
    job['expiry'] = datetime.fromisoformat(job['expiry'])
    job['created_at'] = datetime.fromisoformat(job['created_at'])
    return job

def get_all_active_jobs() -> List[Dict]:
    """Return all jobs that have not expired yet."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM repeat_jobs')
    rows = c.fetchall()
    conn.close()
    jobs = []
    columns = ['job_id', 'creator_id', 'source_chat_id', 'target_ids', 'interval_seconds',
               'expiry', 'auto_delete_seconds', 'created_at', 'media_type', 'media_file_id', 'caption',
               'poll_data', 'text', 'message_count']
    now = datetime.now()
    for row in rows:
        job = dict(zip(columns, row))
        job['expiry'] = datetime.fromisoformat(job['expiry'])
        if job['expiry'] <= now:
            continue
        job['created_at'] = datetime.fromisoformat(job['created_at'])
        if job['poll_data']:
            job['poll_data'] = json.loads(job['poll_data'])
        if job['target_ids']:
            job['target_ids'] = [int(id) for id in job['target_ids'].split(',')]
        jobs.append(job)
    return jobs

def delete_job_from_db(job_id: str):
    """Delete a job and its associated sent messages."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM repeat_jobs WHERE job_id = ?', (job_id,))
    c.execute('DELETE FROM sent_messages WHERE job_id = ?', (job_id,))
    conn.commit()
    conn.close()
    logger.info(f"Deleted job {job_id} from database")

def increment_job_message_count(job_id: str, count: int = 1):
    """Increment the message count of a job."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE repeat_jobs SET message_count = message_count + ? WHERE job_id = ?', (count, job_id))
    conn.commit()
    conn.close()

def get_jobs_for_creator(creator_id: int) -> List[Dict]:
    """Return all jobs created by a specific user (including expired)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM repeat_jobs WHERE creator_id = ?', (creator_id,))
    rows = c.fetchall()
    conn.close()
    jobs = []
    columns = ['job_id', 'creator_id', 'source_chat_id', 'target_ids', 'interval_seconds',
               'expiry', 'auto_delete_seconds', 'created_at', 'media_type', 'media_file_id', 'caption',
               'poll_data', 'text', 'message_count']
    for row in rows:
        job = dict(zip(columns, row))
        job['expiry'] = datetime.fromisoformat(job['expiry'])
        job['created_at'] = datetime.fromisoformat(job['created_at'])
        if job['poll_data']:
            job['poll_data'] = json.loads(job['poll_data'])
        if job['target_ids']:
            job['target_ids'] = [int(id) for id in job['target_ids'].split(',')]
        jobs.append(job)
    return jobs


# ================== SENT MESSAGES FUNCTIONS ==================
def save_sent_message(job_id: str, chat_id: int, message_id: int, delete_at: Optional[datetime] = None, rule_id: int = None):
    """Record a sent message for potential auto-deletion."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO sent_messages (job_id, chat_id, message_id, delete_at, rule_id)
        VALUES (?, ?, ?, ?, ?)
    ''', (job_id, chat_id, message_id, delete_at.isoformat() if delete_at else None, rule_id))
    conn.commit()
    conn.close()

def delete_sent_message_from_db(message_id: int, chat_id: int):
    """Remove a sent message record after deletion."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM sent_messages WHERE message_id = ? AND chat_id = ?', (message_id, chat_id))
    conn.commit()
    conn.close()

def get_expired_messages() -> List[tuple]:
    """Return all messages whose delete_at time has passed."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('SELECT job_id, chat_id, message_id FROM sent_messages WHERE delete_at <= ?', (now,))
    rows = c.fetchall()
    conn.close()
    return rows


# ================== STATS FUNCTIONS ==================
def increment_stat(stat_name: str, by: int = 1):
    """Increment a statistic counter."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE stats SET stat_value = stat_value + ? WHERE stat_name = ?', (by, stat_name))
    if c.rowcount == 0:
        c.execute('INSERT INTO stats (stat_name, stat_value) VALUES (?, ?)', (stat_name, by))
    conn.commit()
    conn.close()

def get_stat(stat_name: str) -> int:
    """Get the current value of a statistic."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT stat_value FROM stats WHERE stat_name = ?', (stat_name,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


# ================== GROUP SETTINGS (WELCOME/GOODBYE/GHOST) ==================
def set_group_welcome(group_id: int, message: str, enabled: bool = True):
    """Set or disable welcome message (text only) for a group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO group_settings (group_id, welcome_enabled, welcome_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            welcome_enabled = ?,
            welcome_message = ?,
            updated_at = ?
    ''', (group_id, 1 if enabled else 0, message, now, now,
          1 if enabled else 0, message, now))
    conn.commit()
    conn.close()
    logger.info(f"Welcome message for group {group_id} set to: {message[:50]}...")

def set_group_goodbye(group_id: int, message: str, enabled: bool = True):
    """Set or disable goodbye message (text only) for a group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO group_settings (group_id, goodbye_enabled, goodbye_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            goodbye_enabled = ?,
            goodbye_message = ?,
            updated_at = ?
    ''', (group_id, 1 if enabled else 0, message, now, now,
          1 if enabled else 0, message, now))
    conn.commit()
    conn.close()
    logger.info(f"Goodbye message for group {group_id} set to: {message[:50]}...")

def get_group_settings(group_id: int) -> Dict[str, Any]:
    """Retrieve all settings for a group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT welcome_enabled, welcome_message, goodbye_enabled, goodbye_message, ghost_mode,
               welcome_media_type, welcome_media_file_id, welcome_caption,
               goodbye_media_type, goodbye_media_file_id, goodbye_caption
        FROM group_settings WHERE group_id = ?
    ''', (group_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            'welcome_enabled': bool(row[0]),
            'welcome_message': row[1],
            'goodbye_enabled': bool(row[2]),
            'goodbye_message': row[3],
            'ghost_mode': bool(row[4]),
            'welcome_media_type': row[5],
            'welcome_media_file_id': row[6],
            'welcome_caption': row[7],
            'goodbye_media_type': row[8],
            'goodbye_media_file_id': row[9],
            'goodbye_caption': row[10],
        }
    else:
        return {
            'welcome_enabled': False,
            'welcome_message': None,
            'goodbye_enabled': False,
            'goodbye_message': None,
            'ghost_mode': False,
            'welcome_media_type': None,
            'welcome_media_file_id': None,
            'welcome_caption': None,
            'goodbye_media_type': None,
            'goodbye_media_file_id': None,
            'goodbye_caption': None,
        }

def set_ghost_mode(group_id: int, enabled: bool):
    """Enable or disable ghost mode for a group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO group_settings (group_id, ghost_mode, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(group_id) DO UPDATE SET
            ghost_mode = ?,
            updated_at = ?
    ''', (group_id, 1 if enabled else 0, now, now, 1 if enabled else 0, now))
    conn.commit()
    conn.close()
    logger.info(f"Ghost mode for group {group_id} set to {enabled}")

def add_ghost_forward(group_id: int, forward_to: int):
    """Register that messages from this group should be forwarded to owner."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO ghost_forward (group_id, forward_to, created_at)
        VALUES (?, ?, ?)
    ''', (group_id, forward_to, now))
    conn.commit()
    conn.close()
    logger.info(f"Ghost forward added for group {group_id} to {forward_to}")

def remove_ghost_forward(group_id: int):
    """Remove ghost forward registration."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM ghost_forward WHERE group_id = ?', (group_id,))
    conn.commit()
    conn.close()
    logger.info(f"Ghost forward removed for group {group_id}")

def is_ghost_mode(group_id: int) -> bool:
    """Check if ghost mode is enabled for a group."""
    settings = get_group_settings(group_id)
    return settings['ghost_mode']

def get_all_ghost_groups() -> List[Dict]:
    """Return list of all groups with ghost forwarding."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT group_id, forward_to FROM ghost_forward')
    rows = c.fetchall()
    conn.close()
    return [{'group_id': row[0], 'forward_to': row[1]} for row in rows]


# ================== RULE FUNCTIONS (AUTO-REPLY) ==================
def add_rule(group_id: int, trigger_type: str, trigger_pattern: str, is_regex: bool,
             reply_template: str, auto_delete_trigger: bool, auto_delete_reply: bool,
             auto_delete_seconds: int, warn_on_trigger: bool, warn_count: int,
             notify_user: bool, exempt_admins: bool, created_by: int,
             reply_media_type: str = None, reply_media_file_id: str = None, reply_caption: str = None) -> int:
    """Insert a new auto-reply rule. Returns rule_id."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO group_rules (
            group_id, trigger_type, trigger_pattern, is_regex, reply_template,
            auto_delete_trigger, auto_delete_reply, auto_delete_seconds,
            warn_on_trigger, warn_count, notify_user, exempt_admins,
            created_by, created_at, reply_media_type, reply_media_file_id, reply_caption
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        group_id, trigger_type, trigger_pattern, 1 if is_regex else 0, reply_template,
        1 if auto_delete_trigger else 0, 1 if auto_delete_reply else 0, auto_delete_seconds,
        1 if warn_on_trigger else 0, warn_count, 1 if notify_user else 0, 1 if exempt_admins else 0,
        created_by, now, reply_media_type, reply_media_file_id, reply_caption
    ))
    rule_id = c.lastrowid
    conn.commit()
    conn.close()
    logger.info(f"New rule {rule_id} added for group {group_id}")
    return rule_id

def get_rules(group_id: int) -> List[Dict]:
    """Return all rules for a specific group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM group_rules WHERE group_id = ? ORDER BY rule_id', (group_id,))
    rows = c.fetchall()
    conn.close()
    columns = ['rule_id', 'group_id', 'trigger_type', 'trigger_pattern', 'is_regex',
               'reply_template', 'auto_delete_trigger', 'auto_delete_reply', 'auto_delete_seconds',
               'warn_on_trigger', 'warn_count', 'notify_user', 'exempt_admins',
               'created_by', 'created_at', 'reply_media_type', 'reply_media_file_id', 'reply_caption']
    rules = []
    for row in rows:
        rule = dict(zip(columns, row))
        rule['is_regex'] = bool(rule['is_regex'])
        rule['auto_delete_trigger'] = bool(rule['auto_delete_trigger'])
        rule['auto_delete_reply'] = bool(rule['auto_delete_reply'])
        rule['warn_on_trigger'] = bool(rule['warn_on_trigger'])
        rule['notify_user'] = bool(rule['notify_user'])
        rule['exempt_admins'] = bool(rule['exempt_admins'])
        rule['created_at'] = datetime.fromisoformat(rule['created_at']) if rule['created_at'] else None
        rules.append(rule)
    return rules

def delete_rule(rule_id: int) -> bool:
    """Delete a rule by its ID."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM group_rules WHERE rule_id = ?', (rule_id,))
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    if success:
        logger.info(f"Rule {rule_id} deleted")
    return success

def get_rule(rule_id: int) -> Optional[Dict]:
    """Retrieve a specific rule by ID."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM group_rules WHERE rule_id = ?', (rule_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    columns = ['rule_id', 'group_id', 'trigger_type', 'trigger_pattern', 'is_regex',
               'reply_template', 'auto_delete_trigger', 'auto_delete_reply', 'auto_delete_seconds',
               'warn_on_trigger', 'warn_count', 'notify_user', 'exempt_admins',
               'created_by', 'created_at', 'reply_media_type', 'reply_media_file_id', 'reply_caption']
    rule = dict(zip(columns, row))
    rule['is_regex'] = bool(rule['is_regex'])
    rule['auto_delete_trigger'] = bool(rule['auto_delete_trigger'])
    rule['auto_delete_reply'] = bool(rule['auto_delete_reply'])
    rule['warn_on_trigger'] = bool(rule['warn_on_trigger'])
    rule['notify_user'] = bool(rule['notify_user'])
    rule['exempt_admins'] = bool(rule['exempt_admins'])
    rule['created_at'] = datetime.fromisoformat(rule['created_at']) if rule['created_at'] else None
    return rule


# ================== WARN FUNCTIONS ==================
def add_warn(user_id: int, group_id: int, rule_id: int, warned_by: int, reason: str = ""):
    """Add a warning record for a user."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''
        INSERT INTO user_warns (user_id, group_id, rule_id, warned_by, warned_at, reason)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, group_id, rule_id, warned_by, now, reason))
    conn.commit()
    conn.close()
    logger.info(f"Warn added for user {user_id} in group {group_id}")

def get_user_warns(user_id: int, group_id: int) -> int:
    """Get total warn count for a user in a specific group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM user_warns WHERE user_id = ? AND group_id = ?', (user_id, group_id))
    count = c.fetchone()[0]
    conn.close()
    return count

def clear_user_warns(user_id: int, group_id: int):
    """Delete all warns for a user in a group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM user_warns WHERE user_id = ? AND group_id = ?', (user_id, group_id))
    conn.commit()
    conn.close()
    logger.info(f"All warns cleared for user {user_id} in group {group_id}")


# ================== BANNED WORDS FUNCTIONS ==================
def add_banned_word(group_id: int, word: str, created_by: int):
    """Add a word to group's banned list."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    try:
        c.execute('''
            INSERT INTO banned_words (group_id, word, created_by, created_at)
            VALUES (?, ?, ?, ?)
        ''', (group_id, word, created_by, now))
        conn.commit()
        logger.info(f"Added banned word '{word}' in group {group_id}")
    except sqlite3.IntegrityError:
        # Word already exists – ignore
        pass
    finally:
        conn.close()

def remove_banned_word(group_id: int, word: str) -> bool:
    """Remove a word from group's banned list."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM banned_words WHERE group_id = ? AND word = ?', (group_id, word))
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    return success

def get_banned_words(group_id: int) -> List[str]:
    """Return list of all banned words for a group."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT word FROM banned_words WHERE group_id = ?', (group_id,))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]
