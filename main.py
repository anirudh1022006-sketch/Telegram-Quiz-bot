import os
from telegram import Update, Poll, PollOption
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import json
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path
from flask import Flask

# Initialize Flask app for Render health check
app = Flask(__name__)

@app.route('/')
def home():
    return "MCQ Quiz Bot is running", 200

# Bot configuration
TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')
QUESTIONS_FILE = os.path.join(Path(__file__).parent, "questions.json")
SCHEDULE_START = 8  # 8 AM
SCHEDULE_END = 20   # 8 PM

# Load or initialize questions
def load_questions():
    try:
        with open(QUESTIONS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"pending": [], "posted": []}

def save_questions(questions):
    with open(QUESTIONS_FILE, "w") as f:
        json.dump(questions, f)

questions = load_questions()

# Bot commands
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Welcome to MCQ Quiz Bot!\n\n"
        "Send me MCQ questions in this format:\n\n"
        "Question text\n"
        "Option 1\n"
        "Option 2\n"
        "Option 3\n"
        "Option 4\n"
        "Correct answer (1-4)\n\n"
        "Example:\n"
        "What is 2+2?\n"
        "3\n"
        "4\n"
        "5\n"
        "6\n"
        "2"
    )

def help_command(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Send MCQ questions in this format:\n\n"
        "Question text\n"
        "Option 1\n"
        "Option 2\n"
        "Option 3\n"
        "Option 4\n"
        "Correct answer (1-4)\n\n"
        "Admin commands:\n"
        "/postnow - Post next question immediately\n"
        "/status - Show pending/posted counts"
    )

def status(update: Update, context: CallbackContext):
    if str(update.effective_chat.id) != ADMIN_CHAT_ID:
        return
    update.message.reply_text(
        f"Pending questions: {len(questions['pending'])}\n"
        f"Posted questions: {len(questions['posted'])}"
    )

def post_now(update: Update, context: CallbackContext):
    if str(update.effective_chat.id) != ADMIN_CHAT_ID:
        return
    post_next_question(context.bot)
    update.message.reply_text("Posted next question!")

def receive_question(update: Update, context: CallbackContext):
    if update.effective_chat.type != "private":
        return
    
    lines = update.message.text.split('\n')
    if len(lines) < 6:
        update.message.reply_text("Error: Not enough lines. Need 6 lines (question + 4 options + correct answer).")
        return
    
    try:
        correct_answer = int(lines[5].strip())
        if correct_answer < 1 or correct_answer > 4:
            raise ValueError
    except ValueError:
        update.message.reply_text("Error: Correct answer must be a number between 1-4.")
        return
    
    question = {
        "text": lines[0].strip(),
        "options": [line.strip() for line in lines[1:5]],
        "correct": correct_answer - 1,  # Convert to 0-based index
        "sender": update.effective_user.id
    }
    
    questions["pending"].append(question)
    save_questions(questions)
    update.message.reply_text("Question received and stored!")

def post_next_question(bot):
    if not questions["pending"]:
        bot.send_message(chat_id=ADMIN_CHAT_ID, text="No more questions to post!")
        return
    
    question = questions["pending"].pop(0)
    
    try:
        message = bot.send_poll(
            chat_id=ADMIN_CHAT_ID,
            question=question["text"],
            options=question["options"],
            type=Poll.QUIZ,
            correct_option_id=question["correct"],
            is_anonymous=False
        )
        
        question["message_id"] = message.message_id
        questions["posted"].append(question)
        save_questions(questions)
    except Exception as e:
        print(f"Error posting question: {e}")
        questions["pending"].insert(0, question)
        save_questions(questions)

def scheduled_posting(context: CallbackContext):
    now = datetime.now().time()
    if SCHEDULE_START <= now.hour < SCHEDULE_END:
        post_next_question(context.bot)

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

def main():
    # Start Flask server in a separate thread for Render
    if os.getenv('RENDER'):
        threading.Thread(target=run_flask, daemon=True).start()

    # Create the Updater and pass it your bot's token.
    updater = Updater(TOKEN)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # Register commands
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("status", status))
    dispatcher.add_handler(CommandHandler("postnow", post_now))
    
    # Register message handler
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, receive_question))

    # Start scheduled posting
    job_queue = updater.job_queue
    job_queue.run_repeating(scheduled_posting, interval=60, first=0)

    # Start the Bot
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
