  import os
import time
import logging
from telegram import Bot
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from threading import Timer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ✅ Get bot token from Render environment variables
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    logger.error("Please set TELEGRAM_TOKEN environment variable.")
    exit(1)

# ✅ Put your channel username or numeric ID here
CHANNEL_ID = "5891731303"  # Example: @edhubquiz
# If your channel is private, use numeric ID like: -1001234567890

POST_INTERVAL = 1800  # 30 minutes in seconds

bot = Bot(token=TOKEN)
mcq_list = []
timer_running = False

def start(update, context):
    update.message.reply_text(
        "Welcome! Send me your MCQs in the format:\n\n"
        "Question?|Option1|Option2|Option3|Option4|CorrectOptionNumber"
    )

def add_mcq(update, context):
    try:
        text = update.message.text.strip()
        parts = text.split("|")
        if len(parts) != 6:
            update.message.reply_text("Invalid format! Please use:\nQuestion?|Opt1|Opt2|Opt3|Opt4|CorrectOptionNumber")
            return
        mcq_list.append(parts)
        update.message.reply_text("MCQ added successfully!")
        start_timer(context)
    except Exception as e:
        update.message.reply_text(f"Error: {e}")

def post_mcq():
    global mcq_list, timer_running
    if mcq_list:
        mcq = mcq_list.pop(0)
        question, opt1, opt2, opt3, opt4, correct = mcq
        bot.send_poll(
            chat_id=CHANNEL_ID,
            question=question,
            options=[opt1, opt2, opt3, opt4],
            type="quiz",
            correct_option_id=int(correct) - 1
        )
        Timer(POST_INTERVAL, post_mcq).start()
    else:
        timer_running = False

def start_timer(context):
    global timer_running
    if not timer_running:
        timer_running = True
        Timer(POST_INTERVAL, post_mcq).start()

def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, add_mcq))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()      
