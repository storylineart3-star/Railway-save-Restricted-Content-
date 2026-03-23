import os
import re
import asyncio
import logging
import time
import requests
from datetime import datetime
from typing import Optional

import aiosqlite
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
DEFAULT_COOLDOWN = int(os.getenv("COOLDOWN", 10))
DEFAULT_AUTO_DELETE = int(os.getenv("AUTO_DELETE", 300))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", 50))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", 200))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== DATABASE =====
DB_PATH = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TEXT,
                last_activity TEXT,
                request_count INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS requests (
                user_id INTEGER,
                timestamp TEXT,
                link TEXT,
                success INTEGER,
                error TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value INTEGER
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                session_string TEXT
            )
        ''')
        await db.commit()

# ===== CONFIG HELPERS =====
async def get_config(key: str, default: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT value FROM config WHERE key = ?', (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default

async def set_config(key: str, value: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, value))
        await db.commit()

async def get_cooldown() -> int:
    return await get_config("cooldown", DEFAULT_COOLDOWN)

async def get_auto_delete() -> int:
    return await get_config("auto_delete", DEFAULT_AUTO_DELETE)

# ===== USER HELPERS =====
async def update_user(user: dict):
    user_id = user["id"]
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO users (user_id, username, first_name, last_name, joined_at, last_activity, request_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_activity = excluded.last_activity,
                request_count = request_count + 1
        ''', (user_id, user.get("username"), user.get("first_name"), user.get("last_name"), now, now))
        await db.commit()

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def log_request(user_id: int, link: str, success: bool, error: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO requests (user_id, timestamp, link, success, error)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, datetime.utcnow().isoformat(), link, 1 if success else 0, error))
        await db.commit()

# ===== SESSION HELPERS =====
async def get_user_session(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT session_string FROM sessions WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def save_user_session(user_id: int, session_string: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR REPLACE INTO sessions (user_id, session_string) VALUES (?, ?)', (user_id, session_string))
        await db.commit()

# ===== TELEGRAM CLIENT MANAGEMENT =====
clients = {}

async def get_client(user_id: int) -> Optional[TelegramClient]:
    if user_id in clients:
        return clients[user_id]
    session_str = await get_user_session(user_id)
    if not session_str:
        return None
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    clients[user_id] = client
    return client

# ===== COMMAND HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update_user(user.to_dict())
    await update.message.reply_text(
        "👋 **Welcome to the Channel Media Saver Bot!**\n\n"
        "This bot uses a user account to fetch messages from public channels.\n"
        "First, use /login to connect your Telegram account.\n\n"
        f"📦 **File limits:**\n"
        f"- ≤{MAX_FILE_MB} MB: sent directly\n"
        f"- {MAX_FILE_MB} MB – {MAX_DOWNLOAD_MB} MB: uploaded to cloud (temporary link)\n"
        f"- >{MAX_DOWNLOAD_MB} MB: rejected\n\n"
        "ℹ️ Use /help for full guide.",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cooldown = await get_cooldown()
    auto_delete = await get_auto_delete()
    await update.message.reply_text(
        f"📘 **GUIDE**\n\n"
        "1. Use /login to connect your Telegram account.\n"
        "2. Send any public Telegram message link.\n\n"
        f"⚠️ **Limits:**\n"
        f"- Cooldown: {cooldown} seconds between requests\n"
        "- Admins have no cooldown\n"
        f"- File size: ≤{MAX_FILE_MB} MB → sent via bot\n"
        f"- {MAX_FILE_MB} MB – {MAX_DOWNLOAD_MB} MB → uploaded to cloud\n"
        f"- >{MAX_DOWNLOAD_MB} MB → rejected\n\n"
        "📌 **Commands:**\n"
        "`/start` `/help` `/myinfo` `/login` `/cancel`\n\n"
        f"Messages auto‑delete after {auto_delete} seconds.",
        parse_mode="Markdown"
    )

async def myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
            user = await cursor.fetchone()
    if not user:
        await update.message.reply_text("No data found. Please send a link first.")
        return
    info = (
        f"👤 **Your Info**\n"
        f"User ID: `{user_id}`\n"
        f"Username: @{user[1] or 'N/A'}\n"
        f"First Name: {user[2] or 'N/A'}\n"
        f"Requests: {user[6]}\n"
        f"Joined: {user[4]}\n"
        f"Last Activity: {user[5]}\n"
        f"Banned: {'Yes' if user[7] else 'No'}"
    )
    await update.message.reply_text(info, parse_mode="Markdown")

# ===== LOGIN CONVERSATION =====
PHONE, CODE, PASSWORD = range(3)

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Send your phone number with country code.\nExample: `+919999999999`", parse_mode="Markdown")
    return PHONE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    context.user_data["phone"] = phone

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(phone)

    context.user_data["client"] = client

    await update.message.reply_text(
        "🔢 Enter the OTP like: `1 2 3 4 5`\n(Spaces are required)",
        parse_mode="Markdown"
    )
    return CODE

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.replace(" ", "")
    client = context.user_data["client"]
    user_id = update.effective_user.id

    try:
        await client.sign_in(context.user_data["phone"], code)
    except SessionPasswordNeededError:
        await update.message.reply_text("🔑 Enter your 2FA password:")
        return PASSWORD

    session = client.session.save()
    await save_user_session(user_id, session)
    clients[user_id] = client
    await update.message.reply_text("✅ **Login successful!** You can now use the bot.", parse_mode="Markdown")
    return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data["client"]
    user_id = update.effective_user.id

    await client.sign_in(password=update.message.text)

    session = client.session.save()
    await save_user_session(user_id, session)
    clients[user_id] = client
    await update.message.reply_text("✅ **Login successful!**", parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Login cancelled.")
    return ConversationHandler.END

# ===== ADMIN COMMANDS =====
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ You are not authorized to use this command.")
            return
        return await func(update, context)
    return wrapper

@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT COUNT(*) FROM users') as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute('SELECT COUNT(*) FROM users WHERE is_banned = 1') as cursor:
            banned_users = (await cursor.fetchone())[0]
        async with db.execute('SELECT COUNT(*) FROM requests') as cursor:
            total_requests = (await cursor.fetchone())[0]
        today = datetime.utcnow().date().isoformat()
        async with db.execute('SELECT COUNT(*) FROM requests WHERE date(timestamp) = ?', (today,)) as cursor:
            today_requests = (await cursor.fetchone())[0]
    cooldown = await get_cooldown()
    auto_delete = await get_auto_delete()
    msg = (
        f"📊 **Bot Statistics**\n"
        f"Total Users: {total_users}\n"
        f"Banned Users: {banned_users}\n"
        f"Total Requests: {total_requests}\n"
        f"Requests Today: {today_requests}\n"
        f"Cooldown: {cooldown} sec\n"
        f"Auto‑delete: {auto_delete} sec"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

@admin_only
async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 0
    if context.args:
        try:
            page = int(context.args[0]) - 1
            if page < 0:
                page = 0
        except ValueError:
            pass
    limit = 10
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT user_id, username, request_count FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?',
            (limit, page * limit)
        ) as cursor:
            users = await cursor.fetchall()
    if not users:
        await update.message.reply_text("No users found.")
        return
    text = "**Users (latest first):**\n"
    for u in users:
        text += f"• `{u[0]}` - @{u[1] or 'N/A'} - {u[2]} reqs\n"
    text += f"\nPage {page+1}. Use `/users {page+2}` for next page."
    await update.message.reply_text(text, parse_mode="Markdown")

@admin_only
async def user_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /user <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
            user = await cursor.fetchone()
    if not user:
        await update.message.reply_text("User not found.")
        return
    info = (
        f"👤 **User Details**\n"
        f"User ID: `{user_id}`\n"
        f"Username: @{user[1] or 'N/A'}\n"
        f"First Name: {user[2] or 'N/A'}\n"
        f"Requests: {user[6]}\n"
        f"Joined: {user[4]}\n"
        f"Last Activity: {user[5]}\n"
        f"Banned: {'Yes' if user[7] else 'No'}"
    )
    await update.message.reply_text(info, parse_mode="Markdown")

@admin_only
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (user_id,))
        await db.commit()
    await update.message.reply_text(f"✅ User {user_id} banned.")

@admin_only
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (user_id,))
        await db.commit()
    await update.message.reply_text(f"✅ User {user_id} unbanned.")

@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        msg = update.message.reply_to_message
        await update.message.reply_text("📢 Starting broadcast...")
        count = 0
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT user_id FROM users WHERE is_banned = 0') as cursor:
                users = await cursor.fetchall()
        for (uid,) in users:
            try:
                await msg.copy(uid)
                count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {uid}: {e}")
        await update.message.reply_text(f"✅ Broadcast sent to {count} users.")
    else:
        if not context.args:
            await update.message.reply_text("Usage: /broadcast <message> or reply to a message with /broadcast")
            return
        text = " ".join(context.args)
        await update.message.reply_text("📢 Starting broadcast...")
        count = 0
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute('SELECT user_id FROM users WHERE is_banned = 0') as cursor:
                users = await cursor.fetchall()
        for (uid,) in users:
            try:
                await context.bot.send_message(uid, text)
                count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {uid}: {e}")
        await update.message.reply_text(f"✅ Broadcast sent to {count} users.")

@admin_only
async def set_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setcooldown <seconds>")
        return
    try:
        seconds = int(context.args[0])
        if seconds < 1:
            raise ValueError
        await set_config("cooldown", seconds)
        await update.message.reply_text(f"✅ Cooldown set to {seconds} seconds.")
    except ValueError:
        await update.message.reply_text("Invalid number. Please provide a positive integer.")

@admin_only
async def set_autodelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setautodelete <seconds>")
        return
    try:
        seconds = int(context.args[0])
        if seconds < 1:
            raise ValueError
        await set_config("auto_delete", seconds)
        await update.message.reply_text(f"✅ Auto‑delete set to {seconds} seconds.")
    except ValueError:
        await update.message.reply_text("Invalid number. Please provide a positive integer.")

# ===== LINK HANDLER =====
last_used = {}

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if "t.me" not in text:
        await update.message.reply_text("❌ Please send a valid Telegram message link.")
        return

    if await is_banned(user_id):
        await update.message.reply_text("⛔ You have been banned from using this bot.")
        return

    await update_user(update.effective_user.to_dict())

    # Cooldown
    if user_id not in ADMIN_IDS:
        cooldown = await get_cooldown()
        now = time.time()
        if user_id in last_used and now - last_used[user_id] < cooldown:
            remaining = int(cooldown - (now - last_used[user_id]))
            await update.message.reply_text(f"⏳ Please wait {remaining} seconds.")
            return
        last_used[user_id] = now

    # Get user's Telethon client
    client = await get_client(user_id)
    if not client:
        await update.message.reply_text("⚠️ You need to login first. Use /login")
        return

    # Parse link
    match = re.search(r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', text)
    if not match:
        await update.message.reply_text("❌ Invalid link format. Use 'Copy Message Link'.")
        await log_request(user_id, text, False, "Invalid link format")
        return

    chat_part = match.group(1)
    msg_id = int(match.group(2))

    # Resolve entity
    try:
        if chat_part.isdigit():
            # Private channel: numeric ID (e.g., c/123456789)
            entity = await client.get_entity(int(f"-100{chat_part}"))
        else:
            # Public channel: username
            entity = await client.get_entity(chat_part)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to resolve channel: {str(e)}")
        await log_request(user_id, text, False, str(e))
        return

    progress_msg = await update.message.reply_text("📥 Fetching message...")

    try:
        message = await client.get_messages(entity, ids=msg_id)
        if not message:
            await progress_msg.edit_text("❌ Message not found.")
            await log_request(user_id, text, False, "Message not found")
            return

        # Text‑only message
        if message.text and not message.media:
            await progress_msg.delete()
            sent = await update.message.reply_text(message.text)
            auto_del = await get_auto_delete()
            asyncio.create_task(auto_delete(context, sent.chat_id, sent.message_id))
            await log_request(user_id, text, True)
            return

        # Media message
        if message.media:
            file_size = message.file.size if message.file else None
            if file_size:
                size_mb = file_size / (1024 * 1024)
                if size_mb > MAX_DOWNLOAD_MB:
                
