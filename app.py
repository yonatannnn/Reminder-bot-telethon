import datetime
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from pymongo import MongoClient
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PORT = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))
WEBHOOK_LISTEN = os.getenv("WEBHOOK_LISTEN", "0.0.0.0")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required.")
if not MONGO_URI:
    raise ValueError("MONGO_URI is required.")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL is required for webhook mode.")


mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_bot"]
plans = db["plans"]

START_KEYBOARD = ReplyKeyboardMarkup([["Start"]], resize_keyboard=True)
application: Optional[Application] = None


def parse_datetime(input_str: str) -> Optional[datetime.datetime]:
    formats = ["%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(input_str, fmt)
        except ValueError:
            continue
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    context.user_data["step"] = "start"
    await update.effective_message.reply_text(
        "Hello! Welcome to the Plan Reminder Bot.\n\n"
        "I'll help you set and track your long-term goals or plans. Click 'Start' to begin!",
        reply_markup=START_KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or update.effective_user is None:
        return

    text = (message.text or "").strip()
    state = context.user_data.get("step")

    if text.lower() == "reset":
        context.user_data.clear()
        context.user_data["step"] = "start"
        await message.reply_text(
            "Reset complete! Let's start over.\n\nClick 'Start' to set a new plan.",
            reply_markup=START_KEYBOARD,
        )
        return

    if state is None:
        return

    if state == "start" and text == "Start":
        context.user_data["step"] = "get_plan"
        await message.reply_text("What is your plan or goal? (e.g., 'Save $100,000 in 2 months')")
        return

    if state == "get_plan":
        context.user_data["plan"] = text
        context.user_data["step"] = "get_datetime"
        await message.reply_text(
            "When should I remind you about this plan? (Format: YYYY-MM-DD HH:MM AM/PM)\n\n"
            "Example: 2026-12-31 11:59 PM\n\n"
            "If you want to restart, type /start."
        )
        return

    if state == "get_datetime":
        reminder_time = parse_datetime(text)
        if reminder_time is None:
            await message.reply_text(
                "Invalid date/time format. Please use: YYYY-MM-DD HH:MM AM/PM.\n\n"
                "Example: 2026-12-31 11:59 PM\n\n"
                "If you want to restart, type /start."
            )
            return

        formatted_time = reminder_time.strftime("%Y-%m-%d %H:%M")
        plans.insert_one(
            {
                "user_id": update.effective_user.id,
                "plan": context.user_data["plan"],
                "reminder_time": formatted_time,
                "notified": False,
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        )
        context.user_data.clear()
        await message.reply_text(
            "Plan saved! I will remind you on the specified date to check your progress.\n\n"
            "Click /start to set another plan."
        )


async def send_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    due_plans = list(plans.find({"reminder_time": now, "notified": False}))

    for plan in due_plans:
        await context.bot.send_message(
            chat_id=plan["user_id"],
            text=(
                f"Plan Reminder: {plan['plan']}\n\n"
                f"Set on: {plan['created_at']}\n"
                f"Reminder Date: {plan['reminder_time']}\n\n"
                "Did you achieve your goal? Take a moment to reflect!\n\n"
                "Click /start to set a new plan."
            ),
        )
        plans.update_one({"_id": plan["_id"]}, {"$set": {"notified": True}})


def build_application() -> Application:
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if telegram_app.job_queue is None:
        raise RuntimeError("Job queue is unavailable. Install python-telegram-bot with job-queue support.")
    telegram_app.job_queue.run_repeating(send_due_reminders, interval=60, first=0)
    return telegram_app


def get_webhook_url() -> str:
    return f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_PATH.lstrip('/')}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global application
    application = build_application()
    webhook_url = get_webhook_url()

    logger.info("Initializing Telegram application")
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("Webhook configured: %s", webhook_url)

    try:
        yield
    finally:
        if application is not None:
            logger.info("Shutting down Telegram application")
            await application.bot.delete_webhook()
            await application.stop()
            await application.shutdown()
        mongo_client.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(f"/{WEBHOOK_PATH.lstrip('/')}")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
) -> dict[str, bool]:
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if application is None:
        raise HTTPException(status_code=503, detail="Application is not ready")

    payload = await request.json()
    update = Update.de_json(payload, application.bot)
    await application.process_update(update)
    return {"ok": True}


def main() -> None:
    logger.info(
        "Starting FastAPI webhook server on %s:%s with webhook %s",
        WEBHOOK_LISTEN,
        WEBHOOK_PORT,
        get_webhook_url(),
    )
    uvicorn.run(app, host=WEBHOOK_LISTEN, port=WEBHOOK_PORT)


if __name__ == "__main__":
    main()
