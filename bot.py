import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from motor.motor_asyncio import AsyncIOMotorClient

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []
DEFAULT_COOLDOWN = int(os.getenv("COOLDOWN", 10))
DEFAULT_AUTO_DELETE = int(os.getenv("AUTO_DELETE", 300))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== DB =====
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["telegram_bot"]
users_col = db["users"]
requests_col = db["requests"]
config_col = db["config"]

# ===== MEMORY =====
last_used = {}  # user_id -> timestamp

# ===== HELPER FUNCTIONS =====
async def get_config(key: str, default: int) -> int:
    doc = await config_col.find_one({"_id": key})
    return doc["value"] if doc else default

async def set_config(key: str, value: int):
    await config_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)

async def get_cooldown() -> int:
    return await get_config("cooldown", DEFAULT_COOLDOWN)

async def get_auto_delete() -> int:
    return await get_config("auto_delete", DEFAULT_AUTO_DELETE)

async def update_user(user: dict):
    user_id = user["id"]
    now = datetime.utcnow()
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": user.get("username"),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "last_activity": now,
            },
            "$setOnInsert": {"joined_at": now, "request_count": 0, "is_banned": False},
            "$inc": {"request_count": 1},
        },
        upsert=True
    )

async def is_banned(user_id: int) -> bool:
    user = await users_col.find_one({"user_id": user_id})
    return user.get("is_banned", False) if user else False

async def log_request(user_id: int, link: str, success: bool, error: str = None):
    await requests_col.insert_one({
        "user_id": user_id,
        "timestamp": datetime.utcnow(),
        "link": link,
        "success": success,
        "error": error
    })

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
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        await update.message.reply_text("No data found. Please send a link first.")
        return
    info = (
        f"👤 **Your Info**\n"
        f"User ID: `{user_id}`\n"
        f"Username: @{user.get('username', 'N/A')}\n"
        f"First Name: {user.get('first_name', 'N/A')}\n"
        f"Requests: {user.get('request_count', 0)}\n"
        f"Joined: {user['joined_at'].strftime('%Y-%m-%d %H:%M')}\n"
        f"Last Activity: {user['last_activity'].strftime('%Y-%m-%d %H:%M')}\n"
        f"Banned: {'Yes' if user.get('is_banned') else 'No'}"
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
    total_users = await users_col.count_documents({})
    banned_users = await users_col.count_documents({"is_banned": True})
    total_requests = await requests_col.count_documents({})
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_requests = await requests_col.count_documents({"timestamp": {"$gte": today}})
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
    cursor = users_col.find().sort("joined_at", -1).skip(page * limit).limit(limit)
    users = await cursor.to_list(length=limit)
    if not users:
        await update.message.reply_text("No users found.")
        return
    text = "**Users (latest first):**\n"
    for u in users:
        text += f"• `{u['user_id']}` - @{u.get('username', 'N/A')} - {u.get('request_count',0)} reqs\n"
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
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        await update.message.reply_text("User not found.")
        return
    info = (
        f"👤 **User Details**\n"
        f"User ID: `{user_id}`\n"
        f"Username: @{user.get('username', 'N/A')}\n"
        f"First Name: {user.get('first_name', 'N/A')}\n"
        f"Requests: {user.get('request_count', 0)}\n"
        f"Joined: {user['joined_at'].strftime('%Y-%m-%d %H:%M')}\n"
        f"Last Activity: {user['last_activity'].strftime('%Y-%m-%d %H:%M')}\n"
        f"Banned: {'Yes' if user.get('is_banned') else 'No'}"
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
    result = await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": True}})
    if result.modified_count:
        await update.message.reply_text(f"✅ User {user_id} banned.")
    else:
        await update.message.reply_text(f"User {user_id} not found or already banned.")

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
    result = await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": False}})
    if result.modified_count:
        await update.message.reply_text(f"✅ User {user_id} unbanned.")
    else:
        await update.message.reply_text(f"User {user_id} not found or already unbanned.")

@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Broadcast via reply or text
    if update.message.reply_to_message:
        msg = update.message.reply_to_message
        await update.message.reply_text("📢 Starting broadcast...")
        count = 0
        async for user in users_col.find({"is_banned": False}):
            try:
                await msg.copy(user["user_id"])
                count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {user['user_id']}: {e}")
        await update.message.reply_text(f"✅ Broadcast sent to {count} users.")
    else:
        if not context.args:
            await update.message.reply_text("Usage: /broadcast <message> or reply to a message with /broadcast")
            return
        text = " ".join(context.args)
        await update.message.reply_text("📢 Starting broadcast...")
        count = 0
        async for user in users_col.find({"is_banned": False}):
            try:
                await context.bot.send_message(user["user_id"], text)
                count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {user['user_id']}: {e}")
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
        # Use webhook mode
        port = int(os.environ.get("PORT", 8080))
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=f"{webhook_url}/{BOT_TOKEN}",
            url_path=BOT_TOKEN
        )
    else:
        # Use polling mode (good for local testing)
        app.run_polling()

if __name__ == "__main__":
    import time
    main()
