from telethon import TelegramClient, events, Button
import os
from pymongo import MongoClient
import datetime
from dotenv import load_dotenv
import asyncio

# Load environment variables
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# Connect to MongoDB
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_bot"]
plans = db["plans"]

# Initialize the Telegram bot
bot = TelegramClient("plan_bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Dictionary to track user states
user_states = {}

# Function to parse date and time with AM/PM support
def parse_datetime(input_str):
    formats = ["%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(input_str, fmt)
        except ValueError:
            continue
    return None

# Start command handler
@bot.on(events.NewMessage(pattern="/start"))
async def start(event):
    user_id = event.sender_id
    user_states[user_id] = {"step": "start"}
    await event.respond(
        "Hello! Welcome to the Plan Reminder Bot.\n\n"
        "I'll help you set and track your long-term goals or plans. Click 'Start' to begin!",
        buttons=[
            Button.text("Start", resize=True),
        ]
    )

# Main message handler
@bot.on(events.NewMessage)
async def handle_message(event):
    user_id = event.sender_id
    if user_id not in user_states:
        return

    state = user_states[user_id]

    if state["step"] == "start" and event.text == "Start":
        await event.respond("What is your plan or goal? (e.g., 'Save $100,000 in 2 months')")
        state["step"] = "get_plan"

    elif state["step"] == "get_plan":
        state["plan"] = event.text
        await event.respond(
            "When should I remind you about this plan? (Format: YYYY-MM-DD HH:MM AM/PM)\n\n"
            "Example: 2023-12-31 11:59 PM\n\n"
            "If you want to restart, type /start."
        )
        state["step"] = "get_datetime"

    elif state["step"] == "get_datetime":
        reminder_time = parse_datetime(event.text)
        if reminder_time:
            formatted_time = reminder_time.strftime("%Y-%m-%d %H:%M")
            plans.insert_one({
                "user_id": user_id,
                "plan": state["plan"],
                "reminder_time": formatted_time,
                "notified": False,
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            })

            del user_states[user_id]

            await event.respond(
                "✅ Plan saved! I will remind you on the specified date to check your progress.\n\n"
                "Click /start to set another plan."
            )
        else:
            await event.respond(
                "❌ Invalid date/time format. Please use the format: YYYY-MM-DD HH:MM AM/PM.\n\n"
                "Example: 2023-12-31 11:59 PM"
            )

    elif event.text.lower() == "reset":
        user_states[user_id] = {"step": "start"}
        await event.respond(
            "🔄 Reset complete! Let's start over.\n\n"
            "Click 'Start' to set a new plan.",
            buttons=[
                Button.text("Start", resize=True),
            ]
        )

# Plan reminder checker function
async def plan_reminder_checker():
    while True:
        now = datetime.datetime.now()
        formatted_now = now.strftime("%Y-%m-%d %H:%M")
        due_plans = plans.find({"reminder_time": formatted_now, "notified": False})

        for plan in due_plans:
            await bot.send_message(
                plan["user_id"],
                f"⏰ Plan Reminder: {plan['plan']}\n\n"
                f"Set on: {plan['created_at']}\n"
                f"Reminder Date: {plan['reminder_time']}\n\n"
                "Did you achieve your goal? Take a moment to reflect!"
            )
            plans.update_one({"_id": plan["_id"]}, {"$set": {"notified": True}})
        
        await asyncio.sleep(60)  # Check every minute

# Run the bot
with bot:
    bot.loop.create_task(plan_reminder_checker())
    print("✅ Bot started and running...")
    bot.run_until_disconnected()

# Close MongoDB connection when the bot stops
mongo_client.close()