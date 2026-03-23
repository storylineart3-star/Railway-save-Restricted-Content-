import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional

import aiosqlite
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
DEFAULT_COOLDOWN = int(os.getenv("COOLDOWN", 10))
DEFAULT_AUTO_DELETE = int(os.getenv("AUTO_DELETE", 300))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== DATABASE =====
DB_PATH = "bot_data.db"

async def init_db():
    """Create tables if they don't exist."""
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

# ===== COMMAND HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update_user(user.to_dict())
    await update.message.reply_text(
        "👋 **Welcome to the Channel Media Saver Bot!**\n\n"
        "Send any Telegram message link from a channel/group where I'm a member.\n\n"
        "ℹ️ Use /help for full guide.",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cooldown = await get_cooldown()
    auto_delete = await get_auto_delete()
    await update.message.reply_text(
        f"📘 **GUIDE**\n\n"
        "1. Add me to the channel/group as a member.\n"
        "2. Send a link to any message in that chat.\n\n"
        f"⚠️ **Limits:**\n"
        f"- Cooldown: {cooldown} seconds between requests\n"
        "- Admins have no cooldown\n\n"
        "📌 **Commands:**\n"
        "`/start` `/help` `/myinfo`\n\n"
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
            if page < 0: page = 0
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
        if seconds < 1: raise ValueError
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
        if seconds < 1: raise ValueError
        await set_config("auto_delete", seconds)
        await update.message.reply_text(f"✅ Auto‑delete set to {seconds} seconds.")
    except ValueError:
        await update.message.reply_text("Invalid number. Please provide a positive integer.")

# ===== LINK HANDLER =====
# In‑memory cooldown storage (simple dict)
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

    # Parse link
    match = re.search(r'https?://t\.me/(?:c/)?([^/]+)/(\d+)', text)
    if not match:
        await update.message.reply_text("❌ Invalid link format. Use 'Copy Message Link'.")
        await log_request(user_id, text, False, "Invalid link format")
        return

    chat_part = match.group(1)
    message_id = int(match.group(2))

    # Build chat identifier
    if chat_part.isdigit():
        chat_id = int(chat_part)
        if chat_id > 0:
            chat_id = int(f"-100{chat_id}")
    else:
        chat_id = f"@{chat_part}"

    progress_msg = await update.message.reply_text("📥 Fetching message...")
    try:
        copied = await context.bot.copy_message(
            chat_id=update.message.chat_id,
            from_chat_id=chat_id,
            message_id=message_id
        )
        auto_del = await get_auto_delete()
        asyncio.create_task(auto_delete(context, copied.chat_id, copied.message_id))
        await progress_msg.delete()
        await log_request(user_id, text, True)
    except Exception as e:
        logger.exception("Error copying message")
        await progress_msg.edit_text(
            f"❌ Error: {str(e)}\n\n"
            "Make sure I am a member of that channel/group and have permission to see messages."
        )
        await log_request(user_id, text, False, str(e))

# ===== AUTO DELETE =====
async def auto_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    await asyncio.sleep(await get_auto_delete())
    try:
        await context.bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

# ===== MAIN =====
def main():
    # Initialize database
    asyncio.run(init_db())

    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myinfo", myinfo))

    # Admin commands
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("user", user_details))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("setcooldown", set_cooldown))
    app.add_handler(CommandHandler("setautodelete", set_autodelete))

    # Link handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    # Determine run mode
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8080))
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}",
            url_path=BOT_TOKEN
        )
    else:
        # Polling (good for local testing)
        app.run_polling()

if __name__ == "__main__":
    import time
    main()
