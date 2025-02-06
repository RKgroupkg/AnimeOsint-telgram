import os
import logging
import traceback
from datetime import datetime
from functools import partial
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import TelegramError

from keep_alive import keep_alive

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
TRACE_MOE_API = "https://api.trace.moe/search"

# Custom keyboard layouts
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 Channel", url="https://t.me/Rkgroup_Bot"),
            InlineKeyboardButton("💬 Support", url="https://t.me/Rkgroup_helpbot?start=start")
        ],
        [
            InlineKeyboardButton("🎯 How to Use", callback_data="how_to_use"),
            InlineKeyboardButton("ℹ️ About", callback_data="about")
        ]
    ])

def get_help_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")
        ]
    ])

# Message templates
START_MESSAGE = """🎌 *Welcome to Anime Screenshot Bot!* 🎌

I can help you find the anime source from screenshots, GIFs, or video clips.

*Features:*
• Fast anime scene recognition
• High accuracy results
• Episode timestamp
• Multiple title formats

Send me an image or use these commands:
/start - Start the bot
/help - Show help
/about - About the bot"""

HELP_MESSAGE = """🎮 *How to Use the Bot* 🎮

1. Send or forward an anime screenshot
2. Wait for the analysis
3. Get detailed results including:
   • Anime titles
   • Episode timestamp
   • Scene preview
   • Similarity score

*Special Options:*
Add these in caption:
• `nocrop` - Disable border cropping
• `mute` - Mute preview video
• `skip` - Skip video preview"""

ABOUT_MESSAGE = """🤖 *About Anime Screenshot Bot* 🤖

A powerful bot that helps you find anime sources using screenshot recognition technology.

*Credits:*
• Powered by trace.moe
• Data from AniList
• Made with ❤️ by @Rkgroup_Bot

Version: 2.0"""

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
            coverImage {
                large
            }
            status
            episodes
            duration
            genres
        }
    }
    """
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                ANILIST_API_URL,
                json={"query": query, "variables": {"id": anilist_id}},
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            return response.json().get("data", {}).get("Media", {})
    except Exception as e:
        logger.error(f"Error fetching AniList info: {e}")
        return {}

async def submit_search(image_bytes: bytes, opts: dict) -> dict:
    """Search image using trace.moe API with file upload."""
    params = {
        "cutBorders": "1" if not opts.get("no_crop") else "",
        "uid": f"tg{opts['from_id']}"
    }
    headers = {"x-trace-key": TRACE_MOE_KEY} if TRACE_MOE_KEY else {}
    files = {"image": ("image.jpg", image_bytes)}
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                TRACE_MOE_API,
                params=params,
                headers=headers,
                files=files,
                timeout=20
            )
            response.raise_for_status()
            data = response.json()
            
            if not data.get("result"):
                return {"text": "❌ *No results found. Try a different image.*"}
            
            result = data["result"][0]
            anilist_info = await get_anilist_info(result["anilist"])
            
            if not anilist_info:
                return {"text": "❌ *Failed to fetch anime details.*"}
            
            titles = [
                anilist_info.get("title", {}).get("native"),
                anilist_info.get("title", {}).get("romaji"),
                anilist_info.get("title", {}).get("english")
            ]
            titles = list(filter(None, titles))
            unique_titles = []
            [unique_titles.append(t) for t in titles if t not in unique_titles]
            
            genres = anilist_info.get("genres", [])
            genres_text = " • ".join(genres[:3]) if genres else "N/A"
            
            text = "🎯 *Anime Found!*\n\n"
            text += "*Titles:*\n" + "\n".join([f"• `{t}`" for t in unique_titles]) + "\n\n"
            text += f"*Episode:* `{result['episode'] or 'Unknown'}`\n"
            text += f"*Timestamp:* `{await format_time(result['from'])}`\n"
            text += f"*Similarity:* `{result['similarity'] * 100:.1f}%`\n\n"
            text += f"*Genres:* `{genres_text}`"
            
            return {
                "text": text,
                "video": f"{result['video']}&size=l",
                "is_adult": anilist_info.get("isAdult", False)
            }
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in submit_search: {e}")
        return {"text": "❌ *API error. Please try again later.*"}
    except Exception as e:
        logger.error(f"Unexpected error in submit_search: {e}")
        return {"text": "❌ *An error occurred. Please try again.*"}

async def download_image_file(message) -> bytes:
    """Download the image/video file from the message and return its bytes."""
    try:
        if message.photo:
            file = await message.photo[-1].get_file()
        elif message.animation:
            file = await message.animation.get_file()
        elif message.video:
            file = await message.video.get_file()
        elif message.document and message.document.mime_type.startswith('image/'):
            file = await message.document.get_file()
        else:
            return None
        
        content = await file.download_as_bytearray()
        return bytes(content)
    except Exception as e:
        logger.error(f"Error downloading image: {e}")
        return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    
    responding_msg = message.reply_to_message or message
    loading_message = None
    
    opts = {
        "no_crop": "nocrop" in (message.caption or "").lower(),
        "mute": "mute" in (message.caption or "").lower(),
        "skip": "skip" in (message.caption or "").lower(),
        "from_id": user.id
    }
    
    try:
        image_bytes = await download_image_file(responding_msg)
        if not image_bytes:
            await message.reply_text(
                "⚠️ Please send an image, GIF, or video.",
                reply_markup=get_help_keyboard()
            )
            return
        
        # Send loading animation
        loading_message = await message.reply_animation(
            animation="https://giffiles.alphacoders.com/121/12113.gif",  # Replace with a reliable URL
            caption="🔍 Analyzing your image..."
        )
        
        result = await submit_search(image_bytes, opts)
        
        # Delete loading message
        await loading_message.delete()
        
        if result.get("is_adult"):
            try:
                await user.send_message("🔞 Adult content detected. Details cannot be shown here.")
            except Exception as e:
                await message.reply_text("🔞 Adult content detected. Please enable PM.")
            return
        
        if result.get("video") and not opts.get("skip"):
            video_url = f"{result['video']}&mute" if opts.get("mute") else result['video']
            await responding_msg.reply_video(
                video=video_url,
                caption=result["text"],
                parse_mode=constants.ParseMode.MARKDOWN
            )
        else:
            await responding_msg.reply_text(
                result["text"],
                parse_mode=constants.ParseMode.MARKDOWN
            )
            
    except Exception as e:
        logger.error(f"Error in handle_message: {e}")
        if loading_message:
            try:
                await loading_message.delete()
            except:
                pass
        await message.reply_text("❌ An error occurred. Please try again later.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        START_MESSAGE,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        HELP_MESSAGE,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=get_help_keyboard()
    )

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /about command."""
    await update.message.reply_text(
        ABOUT_MESSAGE,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=get_help_keyboard()
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "main_menu":
        await query.message.edit_text(
            START_MESSAGE,
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard()
        )
    elif query.data == "how_to_use":
        await query.message.edit_text(
            HELP_MESSAGE,
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=get_help_keyboard()
        )
    elif query.data == "about":
        await query.message.edit_text(
            ABOUT_MESSAGE,
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=get_help_keyboard()
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors during bot execution."""
    tb = traceback.format_exc()
    logger.error(f"Exception: {tb}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ An error occurred. Please try again later.",
            parse_mode=constants.ParseMode.MARKDOWN
        )

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.IMAGE,
        handle_message
    ))
    application.add_error_handler(error_handler)
    
    # Start polling
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    keep_alive()
    main()
