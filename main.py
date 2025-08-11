# main.py
import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse
from starlette.routing import Route
import uvicorn

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ------- CONFIG (from environment) -------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # mandatory
TARGET_CHAT = os.environ.get("TARGET_CHAT")       # channel username or id (e.g. @yourchannel)
POST_INTERVAL_MINUTES = int(os.environ.get("POST_INTERVAL_MINUTES", "30"))
ADMIN_IDS = os.environ.get("ADMIN_IDS", "")       # optional: comma-separated Telegram user ids
PORT = int(os.environ.get("PORT", "8000"))
WEBHOOK_BASE = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")

DB_PATH = os.environ.get("QUIZ_DB_PATH", "quiz_store.db")
# -----------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Conversation state
UPLOAD_QUESTION = 0

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS mcqs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        correct_index INTEGER NOT NULL,
        uploader INTEGER,
        added_ts TEXT,
        posted INTEGER DEFAULT 0,
        posted_ts TEXT
    )
    """)
    conn.commit()
    conn.close()

def add_mcq_to_db(question, options, correct_index, uploader=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO mcqs (question, options, correct_index, uploader, added_ts, posted) VALUES (?, ?, ?, ?, ?, 0)",
        (question, json.dumps(options, ensure_ascii=False), correct_index, uploader, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

def count_unposted():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM mcqs WHERE posted=0")
    c = cur.fetchone()[0]
    conn.close()
    return c

def list_pending(limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, question FROM mcqs WHERE posted=0 ORDER BY id ASC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def mark_posted(mcq_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE mcqs SET posted=1, posted_ts=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), mcq_id))
    conn.commit()
    conn.close()

def reset_claim(mcq_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE mcqs SET posted=0 WHERE id=?", (mcq_id,))
    conn.commit()
    conn.close()

def claim_next_mcq():
    """
    Atomically claim the next unposted mcq and return its details.
    We set posted=2 while it is being posted to avoid duplicates.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT id FROM mcqs WHERE posted=0 ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        mcq_id = row[0]
        cur.execute("UPDATE mcqs SET posted=2 WHERE id=? AND posted=0", (mcq_id,))
        if cur.rowcount == 0:
            conn.commit()
            return None
        cur.execute("SELECT question, options, correct_index FROM mcqs WHERE id=?", (mcq_id,))
        r = cur.fetchone()
        conn.commit()
        return {
            "id": mcq_id,
            "question": r[0],
            "options": json.loads(r[1]),
            "correct_index": int(r[2])
        }
    except Exception as e:
        conn.rollback()
        log.exception("DB claim error: %s", e)
        return None
    finally:
        conn.close()

# ---------- Bot handlers ----------
def is_admin(user_id: int):
    if not ADMIN_IDS:
        return True
    try:
        allowed = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()]
        return user_id in allowed
    except Exception:
        return False

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I post MCQs as Telegram quizzes every 30 minutes.\n\n"
        "Use /upload in a private chat to add a question.\n"
        "/status - pending count\n"
        "/list - show next pending"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Pending unposted MCQs: {count_unposted()}")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_pending(10)
    if not rows:
        await update.message.reply_text("No pending MCQs.")
        return
    text = "Next pending MCQs:\n\n" + "\n".join([f"{r[0]}: {r[1][:80]}{'...' if len(r[1])>80 else ''}" for r in rows])
    await update.message.reply_text(text)

async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please upload MCQs in a private chat with me.")
        return ConversationHandler.END
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("You are not authorized to upload MCQs.")
        return ConversationHandler.END
    await update.message.reply_text(
        "Send the MCQ in this single message format:\n\n"
        "Question line\nOption A\nOption B\nOption C\nOption D\nCorrect option index (0-based: 0 for A, 1 for B, ...)\n\n"
        "You may use 2–10 options. Send /cancel to stop."
    )
    return UPLOAD_QUESTION

async def upload_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 3:
        await update.message.reply_text("Not enough lines. Include question, options, and the correct index.")
        return UPLOAD_QUESTION
    try:
        correct_index = int(lines[-1].strip())
    except ValueError:
        await update.message.reply_text("Last line must be the correct option index (integer, 0-based).")
        return UPLOAD_QUESTION
    question = lines[0].strip()
    options = [l.strip() for l in lines[1:-1]]
    if correct_index < 0 or correct_index >= len(options):
        await update.message.reply_text(f"Correct index {correct_index} out of range for {len(options)} options.")
        return UPLOAD_QUESTION
    add_mcq_to_db(question, options, correct_index, uploader=update.effective_user.id)
    await update.message.reply_text(f"Saved! Pending MCQs: {count_unposted()}")
    return ConversationHandler.END

async def upload_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Upload cancelled.")
    return ConversationHandler.END

# ---------- Posting logic ----------
async def post_next_mcq(application: Application):
    mcq = claim_next_mcq()
    if not mcq:
        log.info("No MCQ to post.")
        return False
    if not TARGET_CHAT:
        log.error("TARGET_CHAT not set. Cannot post.")
        reset_claim(mcq["id"])
        return False
    try:
        bot = application.bot
        await bot.send_poll(
            chat_id=TARGET_CHAT,
            question=mcq["question"],
            options=mcq["options"],
            type="quiz",
            correct_option_id=int(mcq["correct_index"]),
            is_anonymous=False
        )
        mark_posted(mcq["id"])
        log.info("Posted MCQ id=%s", mcq["id"])
        return True
    except Exception as e:
        log.exception("Failed to send poll: %s", e)
        # reset so it can be retried later
        reset_claim(mcq["id"])
        return False

# ---------- Web server (Starlette) ----------
async def make_starlette_app(application: Application):
    async def telegram_endpoint(request: Request):
        data = await request.json()
        # push update into PTB's queue
        await application.update_queue.put(Update.de_json(data, bot=application.bot))
        return Response()

    async def health(request: Request):
        return PlainTextResponse("OK")

    return Starlette(routes=[
        Route("/telegram", telegram_endpoint, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ])

# ---------- Main runner ----------
async def run():
    if not TELEGRAM_TOKEN:
        log.error("Please set TELEGRAM_TOKEN environment variable.")
        return

    init_db()

    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()

    # Add handlers (works with webhook updates)
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("list", list_cmd))

    conv = ConversationHandler(
        entry_points=[CommandHandler("upload", upload_start)],
        states={UPLOAD_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, upload_receive)]},
        fallbacks=[CommandHandler("cancel", upload_cancel)],
        per_user=True,
    )
    application.add_handler(conv)

    # Prepare starlette app and uvicorn server
    starlette_app = await make_starlette_app(application)
    port = PORT
    server = uvicorn.Server(config=uvicorn.Config(app=starlette_app, host="0.0.0.0", port=port, log_level="info"))

    # Build webhook URL
    if WEBHOOK_BASE:
        webhook_url = WEBHOOK_BASE.rstrip("/") + "/telegram"
    else:
        log.error("WEBHOOK_BASE not set (RENDER_EXTERNAL_URL or WEBHOOK_URL). Set WEBHOOK_URL env var.")
        return

    # Start the bot and the webserver together
    async with application:
        # set webhook
        await application.bot.set_webhook(url=webhook_url)
        log.info("Webhook set to %s", webhook_url)

        # start the application (handlers ready)
        await application.start()

        # Start background poster task
        async def poster_loop():
            # post immediately if any pending, then every interval
            while True:
                await post_next_mcq(application)
                await asyncio.sleep(max(1, POST_INTERVAL_MINUTES) * 60)

        poster_task = asyncio.create_task(poster_loop())

        # serve incoming webhook requests (blocks until shutdown)
        try:
            await server.serve()
        finally:
            poster_task.cancel()
            await application.stop()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
