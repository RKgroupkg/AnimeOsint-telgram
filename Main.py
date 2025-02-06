import os
import logging
from datetime import datetime
from functools import partial
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TRACE_MOE_KEY = os.getenv("TRACE_MOE_KEY")
ANILIST_API_URL = os.getenv("ANILIST_API_URL", "https://graphql.anilist.co/")

# Constants
TELEGRAM_API = "https://api.telegram.org/bot"
TRACE_MOE_API = "https://api.trace.moe/search"

async def format_time(seconds: float) -> str:
    """Format seconds into HH:MM:SS"""
    sec_num = int(seconds)
    hours = sec_num // 3600
    minutes = (sec_num % 3600) // 60
    seconds = sec_num % 60
    return f"{hours:02}:{minutes:02}:{seconds:02}"

async def get_anilist_info(anilist_id: int) -> dict:
    """Get anime info from Anilist"""
    query = """
    query($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            idMal
            title {
                native
                romaji
                english
            }
            synonyms
            isAdult
        }
    }
    """
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            ANILIST_API_URL,
            json={"query": query, "variables": {"id": anilist_id}},
            headers={"Content-Type": "application/json"}
        )
        return response.json().get("data", {}).get("Media", {})

async def submit_search(image_url: str, opts: dict) -> dict:
    """Search image using trace.moe API"""
    params = {
        "url": image_url,
        "cutBorders": "1" if not opts.get("no_crop") else "",
        "uid": f"tg{opts['from_id']}"
    }
    
    headers = {"x-trace-key": TRACE_MOE_KEY} if TRACE_MOE_KEY else {}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(TRACE_MOE_API, params=params, headers=headers)
        data = response.json()
        
        if response.status_code != 200 or not data.get("result"):
            return {"text": "`API error, please try again later.`"}
        
        result = data["result"][0]
        anilist_info = await get_anilist_info(result["anilist"])
        
        titles = [
            anilist_info.get("title", {}).get(key)
            for key in ["native", "romaji", "english"]
        ]
        titles = list(filter(None, titles))
        unique_titles = []
        [unique_titles.append(t) for t in titles if t not in unique_titles]
        
        text = "\n".join([f"`{t}`" for t in unique_titles]) + "\n"
        text += f"`{result['filename'].replace('`', '``')}`\n"
        text += f"`{await format_time(result['from'])}`\n"
        text += f"`{result['similarity'] * 100:.1f}% similarity`"
        
        return {
            "text": text,
            "video": f"{result['video']}&size=l",
            "is_adult": anilist_info.get("isAdult", False)
        }

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages"""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    
    # Check if message is a reply
    responding_msg = message.reply_to_message or message
    
    # Get search options
    opts = {
        "no_crop": "nocrop" in (message.caption or "").lower(),
        "mute": "mute" in (message.caption or "").lower(),
        "skip": "skip" in (message.caption or "").lower(),
        "from_id": user.id
    }
    
    # Try to get image URL
    image_url = await get_image_url(responding_msg)
    
    if not image_url:
        await send_help_message(message, chat.id)
        return
    
    await context.bot.set_message_reaction(
        chat_id=chat.id,
        message_id=message.message_id,
        reaction=[{"type": "emoji", "emoji": "👌"}]
    )
    
    # Send typing action
    await context.bot.send_chat_action(chat.id, "typing")
    
    # Submit search
    result = await submit_search(image_url, opts)
    
    await context.bot.set_message_reaction(
        chat_id=chat.id,
        message_id=message.message_id,
        reaction=[{"type": "emoji", "emoji": "👍"}]
    )
    
    # Handle adult content
    if result.get("is_adult"):
        await message.reply_text("Adult content detected. Please contact me privately.")
        return
    
    # Send results
    if result.get("video") and not opts.get("skip"):
        video_url = f"{result['video']}&mute" if opts.get("mute") else result['video']
        await message.reply_video(
            video=video_url,
            caption=result["text"],
            parse_mode="Markdown",
            reply_to_message_id=responding_msg.message_id
        )
    else:
        await message.reply_text(
            result["text"],
            parse_mode="Markdown",
            reply_to_message_id=responding_msg.message_id
        )

async def get_image_url(message) -> str:
    """Extract image URL from message"""
    if message.photo:
        return (await message.photo[-1].get_file()).file_url
    if message.animation:
        return (await message.animation.get_file()).file_url
    if message.video and message.video.thumbnail:
        return (await message.video.thumbnail.get_file()).file_url
    if message.document and message.document.thumbnail:
        return (await message.document.thumbnail.get_file()).file_url
    return ""

async def send_help_message(message, chat_id):
    """Send help message with inline buttons"""
    keyboard = [
        [
            InlineKeyboardButton("Channel", url="https://t.me/Rkgroup_Bot"),
            InlineKeyboardButton("Support", url="https://t.me/Rkgroup_helpbot?start=start")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text(
        "You can send/forward anime screenshots to me.",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command"""
    await send_help_message(update.message, update.effective_chat.id)

def main() -> None:
    """Start the bot"""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.IMAGE,
        handle_message
    ))

    # Start polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
