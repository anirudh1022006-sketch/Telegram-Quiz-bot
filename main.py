# main.py
import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- CONFIG (from environment) ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # set this in Render
TARGET_CHAT = os.environ.get("TARGET_CHAT")       # e.g. @yourchannel OR -1001234567890
PORT = int(os.environ.get("PORT", "8000"))
WEBHOOK_BASE = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
# Posting window and timezone
TZ = ZoneInfo("Asia/Kolkata")  # uses India time
POST_WINDOW_START_HOUR = 8     # 08:00
POST_WINDOW_END_HOUR = 20      # 20:00 (stops at 20:00)
POST_INTERVAL_MINUTES = int(os.environ.get("POST_INTERVAL_MINUTES", "30"))
DB_PATH = os.environ.get("QUIZ_DB_PATH", "quiz_store.db")
# ------------------------------------------------

if not TELEGRAM_TOKEN:
    log.error("Please set TELEGRAM_TOKEN environment variable.")
    raise SystemExit("TELEGRAM_TOKEN not set")

if not TARGET_CHAT:
    log.error("Please set TARGET_CHAT environment variable (e.g. @yourchannel or -1001234567890).")
    raise SystemExit("TARGET_CHAT not set")

# Conversation state
UPLOAD_QUESTION = 0

# ---------- Database helpers ----------
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
        (question, json.dumps(options, ensure_ascii=False), correct_index, uploader, datetime.now(ZoneInfo("UTC")).isoformat())
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

def claim_next_mcq():
    """
    Atomically claim the next unposted MCQ by setting posted=2 (in-progress), return details or None.
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

def mark_posted(mcq_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE mcqs SET posted=1, posted_ts=? WHERE id=?", (datetime.now(ZoneInfo("UTC")).isoformat(), mcq_id))
    conn.commit()
    conn.close()

def reset_claim(mcq_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE mcqs SET posted=0 WHERE id=? AND posted=2", (mcq_id,))
    conn.commit()
    conn.close()

# ---------- Bot command handlers ----------
def format_short(text, n=80):
    return text if len(text) <= n else text[:n] + "..."

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I post MCQ quizzes on a schedule.\n\n"
        "Commands (private chat):\n"
        "/upload - upload one MCQ\n"
        "/status - pending count\n"
        "/list - list next pending\n\n"
        "Upload format (use /upload then paste):\n"
        "Question line\nOption A\nOption B\nOption C\nOption D\nCorrect option index (0-based)\n\n"
        "Example:\nWhich gas do plants use for photosynthesis?\nOxygen\nNitrogen\nCarbon dioxide\nHydrogen\n2"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Pending unposted MCQs: {count_unposted()}")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_pending(10)
    if not rows:
        await update.message.reply_text("No pending MCQs.")
        return
    text = "Next pending MCQs:\n\n" + "\n".join([f"{r[0]}: {format_short(r[1])}" for r in rows])
    await update.message.reply_text(text)

async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please upload MCQs in a private chat with the bot.")
        return ConversationHandler.END
    await update.message.reply_text(
        "Send the MCQ in a single message using this format:\n\n"
        "Question line\nOption A\nOption B\nOption C\nOption D\nCorrect option index (0-based)\n\n"
        "You may use 2–10 options. Send /cancel to cancel."
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

# ---------- Posting / scheduling ----------
def next_half_hour(t: datetime):
    # returns next half-hour boundary after time t (tz-aware)
    if t.minute < 30:
        nxt = t.replace(minute=30, second=0, microsecond=0)
    else:
        # move to next hour's 00
        nxt = (t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return nxt

async def poster_loop(application: Application):
    """
    Main loop that waits until next half-hour boundary and posts one MCQ if within posting window.
    """
    await asyncio.sleep(2)  # short startup delay
    while True:
        now = datetime.now(TZ)
        # compute next boundary
        next_run = next_half_hour(now)
        # if next_run is earlier than now (shouldn't happen) add 30m
        if next_run <= now:
            next_run += timedelta(minutes=30)

        # If next_run is within today's posting window (08:00 <= run < 20:00)
        start_today = next_run.replace(hour=POST_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
        end_today = next_run.replace(hour=POST_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)

        if start_today <= next_run < end_today:
            wait_seconds = (next_run - now).total_seconds()
            log.info("Next post scheduled at %s (local). Sleeping %s seconds", next_run.isoformat(), int(wait_seconds))
            await asyncio.sleep(wait_seconds)
            # Time to post one MCQ
            try:
                mcq = claim_next_mcq()
                if not mcq:
                    log.info("No pending MCQ to post at %s", next_run.isoformat())
                    continue
                # send via bot
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
                log.info("Posted MCQ id=%s at %s", mcq["id"], datetime.now(TZ).isoformat())
            except Exception as e:
                log.exception("Error while posting MCQ: %s", e)
                # if claimed but failed, reset claim so it can be retried later
                try:
                    if mcq and mcq.get("id"):
                        reset_claim(mcq["id"])
                except Exception:
                    pass
                # wait a small time before continuing to avoid busy loop
                await asyncio.sleep(5)
        else:
            # compute next day's start (08:00)
            if next_run >= end_today:
                # schedule next day's start at 08:00
                tomorrow = (next_run + timedelta(days=1)).replace(hour=POST_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
                wait_seconds = (tomorrow - now).total_seconds()
                log.info("Outside posting window. Next posting window starts at %s. Sleeping %s seconds", tomorrow.isoformat(), int(wait_seconds))
                await asyncio.sleep(wait_seconds)
            else:
                # next_run is before today's start (e.g. early morning); sleep until start_today
                wait_seconds = (start_today - now).total_seconds()
                log.info("Waiting until posting window start at %s. Sleeping %s seconds", start_today.isoformat(), int(wait_seconds))
                await asyncio.sleep(max(0, wait_seconds))

# ---------- Starlette webhook app to forward updates to PTB ----------
async def make_starlette_app(application: Application):
    async def telegram_endpoint(request: Request):
        data = await request.json()
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
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handlers
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

    # Prepare starlette + server
    starlette_app = await make_starlette_app(application)
    server = uvicorn.Server(config=uvicorn.Config(app=starlette_app, host="0.0.0.0", port=PORT, log_level="info"))

    # Build webhook URL
    if WEBHOOK_BASE:
        webhook_url = WEBHOOK_BASE.rstrip("/") + "/telegram"
    else:
        log.error("WEBHOOK_BASE not set. Set WEBHOOK_URL or use Render which provides RENDER_EXTERNAL_URL.")
        raise SystemExit("WEBHOOK URL not configured")

    async with application:
        # set webhook
        await application.bot.set_webhook(url=webhook_url)
        log.info("Webhook set to %s", webhook_url)

        # start the application handlers
        await application.start()

        # start poster loop
        poster_task = asyncio.create_task(poster_loop(application))

        # run the starlette server (this blocks until termination)
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
