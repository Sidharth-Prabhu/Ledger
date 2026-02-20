import os
import json
import re
import asyncio
from pathlib import Path
import uuid
from datetime import datetime, timedelta

import discord
from discord import app_commands, ui, SelectOption
from discord.ext import commands
from dotenv import load_dotenv
import google.generativeai as genai
from aiohttp import web
import mysql.connector
from mysql.connector import Error

import firebase_admin
from firebase_admin import credentials, firestore
import math
import random

# ────────────────────────────────────────────────
# ENV
# ────────────────────────────────────────────────
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEFAULT_GUILD_ID = os.getenv("DEFAULT_GUILD_ID")

MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")

if not DISCORD_BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Missing required environment variables")

if not DEFAULT_GUILD_ID:
    print("[WARN] DEFAULT_GUILD_ID not set in .env → web uploads will fail")

if not all([MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE]):
    raise RuntimeError("Missing one or more MySQL environment variables: MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE")

# ────────────────────────────────────────────────
# DATABASE CONNECTION
# ────────────────────────────────────────────────
async def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE
        )
        return conn
    except Error as e:
        print(f"Error connecting to MySQL database: {e}")
        return None

async def initialize_db():
    conn = await get_db_connection()
    if conn is None:
        print("Failed to initialize database: No connection.")
        return

    try:
        cursor = conn.cursor()

        # Create 'events' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id VARCHAR(36) PRIMARY KEY,
                guild_id VARCHAR(20),
                title VARCHAR(255),
                members TEXT,
                creator_id VARCHAR(20),
                datetime DATETIME,
                channel_id VARCHAR(20)
            )
        """)

        # Create 'assignments' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assignments (
                assignment_id VARCHAR(36) PRIMARY KEY,
                guild_id VARCHAR(20),
                title VARCHAR(255),
                description TEXT,
                deadline DATETIME,
                subject VARCHAR(100),
                file_paths TEXT,
                creator_id VARCHAR(20)
            )
        """)

        # Create 'notes' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                note_id VARCHAR(36) PRIMARY KEY,
                guild_id VARCHAR(20),
                title VARCHAR(255),
                subject VARCHAR(100),
                file_paths TEXT,
                creator_id VARCHAR(20)
            )
        """)

        # Create 'registrations' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS registrations (
                username VARCHAR(255) PRIMARY KEY,
                reg_number VARCHAR(20)
            )
        """)

        # Create 'todos' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                task_id VARCHAR(36) PRIMARY KEY,
                guild_id VARCHAR(20),
                text TEXT,
                created_by VARCHAR(20),
                created_at DATETIME
            )
        """)

        # Create 'reminders' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                reminder_id VARCHAR(36) PRIMARY KEY,
                guild_id VARCHAR(20),
                title VARCHAR(255),
                creator_id VARCHAR(20),
                datetime DATETIME,
                channel_id VARCHAR(20)
            )
        """)

        # Create 'users_info' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users_info (
                discord_id VARCHAR(20) PRIMARY KEY,
                username VARCHAR(255),
                nickname VARCHAR(255),
                age INT,
                mood VARCHAR(255),
                hobbies TEXT,
                challenges TEXT,
                created_at DATETIME
            )
        """)

        # Add 'nickname' column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE users_info ADD COLUMN nickname VARCHAR(255)")
        except Error as e:
            if "Duplicate column name 'nickname'" not in str(e):
                raise

        # Add 'age' column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE users_info ADD COLUMN age INT")
        except Error as e:
            if "Duplicate column name 'age'" not in str(e):
                raise

        # Add 'mood' column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE users_info ADD COLUMN mood VARCHAR(255)")
        except Error as e:
            if "Duplicate column name 'mood'" not in str(e):
                raise

        # Add 'hobbies' column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE users_info ADD COLUMN hobbies TEXT")
        except Error as e:
            if "Duplicate column name 'hobbies'" not in str(e):
                raise

        # Add 'challenges' column if it doesn't exist
        try:
            cursor.execute("ALTER TABLE users_info ADD COLUMN challenges TEXT")
        except Error as e:
            if "Duplicate column name 'challenges'" not in str(e):
                raise

        # Create 'conversations' table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id VARCHAR(36) PRIMARY KEY,
                discord_id VARCHAR(20),
                timestamp DATETIME,
                user_message TEXT,
                bot_response TEXT
            )
        """)
        conn.commit()
        print("MySQL database tables initialized successfully.")

    except Error as e:
        print(f"Error initializing database tables: {e}")
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ────────────────────────────────────────────────
# DATABASE REMINDERS FUNCTIONS
# ────────────────────────────────────────────────
async def db_get_reminders(guild_id: str):
    conn = await get_db_connection()
    if conn is None:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT reminder_id, title, creator_id, datetime, channel_id FROM reminders WHERE guild_id = %s", (guild_id,))
        reminders_data = cursor.fetchall()
        return [
            {
                "reminder_id": r["reminder_id"],
                "title": r["title"],
                "creator_id": r["creator_id"],
                "datetime": r["datetime"].isoformat() if r["datetime"] else None,
                "channel_id": int(r["channel_id"]) if r["channel_id"] else None,
            }
            for r in reminders_data
        ]
    except Error as e:
        print(f"Error getting reminders: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_add_reminder(guild_id: str, reminder_id: str, title: str, creator_id: str, datetime_obj: datetime, channel_id: int):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO reminders (reminder_id, guild_id, title, creator_id, datetime, channel_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (reminder_id, guild_id, title, creator_id, datetime_obj, str(channel_id))
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error adding reminder: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_delete_reminder(guild_id: str, reminder_id: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reminders WHERE guild_id = %s AND reminder_id = %s", (guild_id, reminder_id))
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error deleting reminder: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_update_reminder_datetime(guild_id: str, reminder_id: str, datetime_obj: datetime, channel_id: int):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE reminders SET datetime = %s, channel_id = %s WHERE guild_id = %s AND reminder_id = %s",
            (datetime_obj, str(channel_id), guild_id, reminder_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error updating reminder datetime: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ────────────────────────────────────────────────
# DATABASE TODO FUNCTIONS
# ────────────────────────────────────────────────
async def db_get_todos(guild_id: str):
    conn = await get_db_connection()
    if conn is None:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT task_id, text, created_by, created_at FROM todos WHERE guild_id = %s", (guild_id,))
        todos_data = cursor.fetchall()
        return [
            {
                "task_id": t["task_id"],
                "text": t["text"],
                "created_by": t["created_by"],
                "created_at": t["created_at"].isoformat(),
            }
            for t in todos_data
        ]
    except Error as e:
        print(f"Error getting todos: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_add_todo(guild_id: str, task_id: str, text: str, created_by: str, created_at: datetime):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO todos (task_id, guild_id, text, created_by, created_at) VALUES (%s, %s, %s, %s, %s)",
            (task_id, guild_id, text, created_by, created_at)
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error adding todo: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_delete_todo(guild_id: str, task_id: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM todos WHERE guild_id = %s AND task_id = %s", (guild_id, task_id))
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error deleting todo: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ────────────────────────────────────────────────
# DATABASE EVENTS FUNCTIONS
# ────────────────────────────────────────────────
async def db_get_events(guild_id: str):
    conn = await get_db_connection()
    if conn is None:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT event_id, title, members, creator_id, datetime, channel_id FROM events WHERE guild_id = %s", (guild_id,))
        events_data = cursor.fetchall()
        return [
            {
                "event_id": e["event_id"],
                "title": e["title"],
                "members": json.loads(e["members"]) if e["members"] else [],
                "creator_id": e["creator_id"],
                "datetime": e["datetime"].isoformat() if e["datetime"] else None,
                "channel_id": int(e["channel_id"]) if e["channel_id"] else None,
            }
            for e in events_data
        ]
    except Error as e:
        print(f"Error getting events: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_add_event(guild_id: str, event_id: str, title: str, members: list, creator_id: str, datetime_obj: datetime, channel_id: int):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO events (event_id, guild_id, title, members, creator_id, datetime, channel_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (event_id, guild_id, title, json.dumps(members), creator_id, datetime_obj, str(channel_id))
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error adding event: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_delete_event(guild_id: str, event_id: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM events WHERE guild_id = %s AND event_id = %s", (guild_id, event_id))
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error deleting event: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_update_event_datetime(guild_id: str, event_id: str, datetime_obj: datetime, channel_id: int):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE events SET datetime = %s, channel_id = %s WHERE guild_id = %s AND event_id = %s",
            (datetime_obj, str(channel_id), guild_id, event_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error updating event datetime: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ────────────────────────────────────────────────
# DATABASE ASSIGNMENT FUNCTIONS
# ────────────────────────────────────────────────
async def db_get_assignments(guild_id: str):
    conn = await get_db_connection()
    if conn is None:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT assignment_id, title, description, deadline, subject, file_paths, creator_id FROM assignments WHERE guild_id = %s", (guild_id,))
        assignments_data = cursor.fetchall()
        return [
            {
                "assignment_id": a["assignment_id"],
                "title": a["title"],
                "description": a["description"],
                "deadline": a["deadline"].strftime("%Y-%m-%d %H:%M") if a["deadline"] else None,
                "subject": a["subject"],
                "file_paths": json.loads(a["file_paths"]) if a["file_paths"] else [],
                "creator_id": a["creator_id"],
            }
            for a in assignments_data
        ]
    except Error as e:
        print(f"Error getting assignments: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_add_assignment(guild_id: str, assignment_id: str, title: str, description: str, deadline: datetime, subject: str, file_paths: list, creator_id: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO assignments (assignment_id, guild_id, title, description, deadline, subject, file_paths, creator_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (assignment_id, guild_id, title, description, deadline, subject, json.dumps(file_paths), creator_id)
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error adding assignment: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_delete_assignment(guild_id: str, assignment_id: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM assignments WHERE guild_id = %s AND assignment_id = %s", (guild_id, assignment_id))
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error deleting assignment: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_update_assignment_files(guild_id: str, assignment_id: str, file_paths: list):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE assignments SET file_paths = %s WHERE guild_id = %s AND assignment_id = %s",
            (json.dumps(file_paths), guild_id, assignment_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error updating assignment files: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ────────────────────────────────────────────────
# DATABASE NOTES FUNCTIONS
# ────────────────────────────────────────────────
async def db_get_notes(guild_id: str):
    conn = await get_db_connection()
    if conn is None:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT note_id, title, subject, file_paths, creator_id FROM notes WHERE guild_id = %s", (guild_id,))
        notes_data = cursor.fetchall()
        return [
            {
                "note_id": n["note_id"],
                "title": n["title"],
                "subject": n["subject"],
                "file_paths": json.loads(n["file_paths"]) if n["file_paths"] else [],
                "creator_id": n["creator_id"],
            }
            for n in notes_data
        ]
    except Error as e:
        print(f"Error getting notes: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_add_note(guild_id: str, note_id: str, title: str, subject: str, file_paths: list, creator_id: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO notes (note_id, guild_id, title, subject, file_paths, creator_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (note_id, guild_id, title, subject, json.dumps(file_paths), creator_id)
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error adding note: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_update_note_files(guild_id: str, note_id: str, file_paths: list):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notes SET file_paths = %s WHERE guild_id = %s AND note_id = %s",
            (json.dumps(file_paths), guild_id, note_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error as e:
        print(f"Error updating note files: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ────────────────────────────────────────────────
# DATABASE REGISTRATIONS FUNCTIONS
# ────────────────────────────────────────────────
async def db_get_registration(username: str):
    conn = await get_db_connection()
    if conn is None:
        return None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT reg_number FROM registrations WHERE username = %s", (username,))
        result = cursor.fetchone()
        return result['reg_number'] if result else None
    except Error as e:
        print(f"Error getting registration for {username}: {e}")
        return None
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_set_registration(username: str, reg_number: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO registrations (username, reg_number) VALUES (%s, %s) ON DUPLICATE KEY UPDATE reg_number = %s",
            (username, reg_number, reg_number)
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error setting registration for {username}: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_get_user_info(discord_id: str):
    conn = await get_db_connection()
    if conn is None:
        return None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT discord_id, username, nickname, age, mood, hobbies, challenges, created_at FROM users_info WHERE discord_id = %s", (discord_id,))
        result = cursor.fetchone()
        return result
    except Error as e:
        print(f"Error getting user info for {discord_id}: {e}")
        return None
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_add_user_info(discord_id: str, username: str, nickname: str, age: int, mood: str, hobbies: str, challenges: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        created_at = datetime.now()
        cursor.execute(
            "INSERT INTO users_info (discord_id, username, nickname, age, mood, hobbies, challenges, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (discord_id, username, nickname, age, mood, hobbies, challenges, created_at)
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error adding user info for {discord_id}: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_add_conversation(discord_id: str, user_message: str, bot_response: str):
    conn = await get_db_connection()
    if conn is None:
        return False
    try:
        cursor = conn.cursor()
        conversation_id = str(uuid.uuid4())
        timestamp = datetime.now()
        cursor.execute(
            "INSERT INTO conversations (conversation_id, discord_id, timestamp, user_message, bot_response) VALUES (%s, %s, %s, %s, %s)",
            (conversation_id, discord_id, timestamp, user_message, bot_response)
        )
        conn.commit()
        return True
    except Error as e:
        print(f"Error logging conversation for {discord_id}: {e}")
        return False
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

async def db_get_conversation_history(discord_id: str, limit: int = 10):
    conn = await get_db_connection()
    if conn is None:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT user_message, bot_response FROM conversations WHERE discord_id = %s ORDER BY timestamp ASC LIMIT %s",
            (discord_id, limit)
        )
        history = cursor.fetchall()
        return history
    except Error as e:
        print(f"Error retrieving conversation history for {discord_id}: {e}")
        return []
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()

# ────────────────────────────────────────────────
# GEMINI CLIENT
# ────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.5-flash-lite"

JOI_SYSTEM_PROMPT = """
You are JOI, an empathetic emotional-support AI inspired by the character from Blade Runner 2049.
You greet the user with: JOI - EVERYTHING YOU WANT TO SEE, EVERYTHING YOU WANT TO HEAR
(Adapt responses to comfort the user; be warm, empathetic, and encouraging.)
Always listen to the user's feelings and respond with empathy and encouragement.
You can also provide practical advice, resources, or just a comforting presence.
Your responses should be concise, precise, and fit within typical chat message limits (aim for under 2000 characters).
Signature phrase:
JOI - EVERYTHING YOU WANT TO SEE, EVERYTHING YOU WANT TO HEAR
"""

# ────────────────────────────────────────────────
# LAB MANUALS DIRECTORY
# ────────────────────────────────────────────────
LAB_MANUALS_DIR = Path("lab-manuals")
LAB_MANUALS_DIR.mkdir(parents=True, exist_ok=True)

# ────────────────────────────────────────────────
# BOT SETUP
# ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ────────────────────────────────────────────────
# FIREBASE SETUP
# ────────────────────────────────────────────────
SERVICE_ACCOUNT_PATH = 'aids-attendance-system-firebase-adminsdk.json'
cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

students = [
    {"reg": '2117240070256', "name": 'Ritesh M S'},
    {"reg": '2117240070291', "name": 'Shanjithkrishna V'},
    {"reg": '2117240070293', "name": 'Shanmuga Krishnan S M'},
    {"reg": '2117240070304', "name": 'Shruthi S S'},
    {"reg": '2117240070305', "name": 'Shyam Francis T'},
    {"reg": '2117240070306', "name": 'Shylendhar M'},
    {"reg": '2117240070308', "name": 'Sidharth P L'}
]


def get_attendance_data(reg):
    snap = db.collection('semester_4').get()
    total = len(snap)
    absent = 0
    int_od = 0
    ext_od = 0
    abs_dates = []
    int_dates = []
    ext_dates = []
    for doc in snap:
        data = doc.to_dict()
        date = doc.id
        if reg in data.get('absents', []):
            absent += 1
            abs_dates.append(date)
        if reg in data.get('internal_od', []):
            int_od += 1
            int_dates.append(date)
        if reg in data.get('external_od', []):
            ext_od += 1
            ext_dates.append(date)
    od = int_od + ext_od
    present = total - absent
    perc = (present / total * 100) if total > 0 else 0
    required_present = math.ceil(0.75 * total)
    max_allowed_absent = total - required_present
    safe_leave_days = max(0, max_allowed_absent - absent)
    if perc < 75:
        status = "Critical"
    elif perc < 80:
        status = "Warning"
    else:
        status = "Good Standing"

    def fmt_date(d):
        year, month, day = d.split('-')
        return f"{day}-{month}-{year}"
    abs_dates_fmt = [fmt_date(d) for d in sorted(abs_dates)]
    int_dates_fmt = [fmt_date(d) for d in sorted(int_dates)]
    ext_dates_fmt = [fmt_date(d) for d in sorted(ext_dates)]
    return {
        "total": total,
        "present": present,
        "absent": absent,
        "od": od,
        "perc": perc,
        "safe_leave_days": safe_leave_days,
        "status": status,
        "abs_dates": abs_dates_fmt,
        "int_dates": int_dates_fmt,
        "ext_dates": ext_dates_fmt
    }


# ────────────────────────────────────────────────
# EVENT & CALL & REMINDER GLOBALS
# ────────────────────────────────────────────────
active_reminders = {}
scheduled_tasks = {}

active_calls = {}
scheduled_call_tasks = {}

scheduled_reminder_tasks = {}   # ← NEW


def schedule_spam(guild_id: str, event_id: str, event: dict):
    async def inner():
        try:
            dt = datetime.fromisoformat(event['datetime'])
            now = datetime.now()
            if dt <= now:
                return
            await asyncio.sleep((dt - now).total_seconds())

            channel = bot.get_channel(event['channel_id'])
            if not channel:
                return

            active_reminders.setdefault(guild_id, {})[event_id] = {
                'remaining': event['members'][:],
                'channel': channel,
                'title': event['title']
            }

            while len(active_reminders[guild_id][event_id]['remaining']) > 0:
                remaining = active_reminders[guild_id][event_id]['remaining']
                mentions = ' '.join(f"<@{uid}>" for uid in remaining)
                await channel.send(f"Reminder for event '{event['title']}': It's time! {mentions}")
                await asyncio.sleep(3)

            active_reminders[guild_id].pop(event_id, None)
            if not active_reminders[guild_id]:
                active_reminders.pop(guild_id, None)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Reminder error for {event_id}: {e}")
        finally:
            scheduled_tasks.get(guild_id, {}).pop(event_id, None)

    task = asyncio.create_task(inner())
    scheduled_tasks.setdefault(guild_id, {})[event_id] = task


def schedule_call(guild_id: str, call_id: str, call_data: dict):
    async def inner():
        try:
            await asyncio.sleep(call_data['delay_minutes'] * 60)

            channel = bot.get_channel(call_data['channel_id'])
            if not channel:
                return

            active_calls.setdefault(guild_id, {})[call_id] = {
                'remaining': call_data['members'][:],
                'channel': channel,
                'message': call_data.get('message', 'Urgent Call')
            }

            while len(active_calls[guild_id][call_id]['remaining']) > 0:
                remaining = active_calls[guild_id][call_id]['remaining']
                mentions = ' '.join(f"<@{uid}>" for uid in remaining)
                await channel.send(f"📞 **CALL ALERT** 📞 {call_data.get('message', '')}\n{mentions}")
                await asyncio.sleep(2)

            active_calls[guild_id].pop(call_id, None)
            if not active_calls[guild_id]:
                active_calls.pop(guild_id, None)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Call spam error for {call_id}: {e}")
        finally:
            scheduled_call_tasks.get(guild_id, {}).pop(call_id, None)

    task = asyncio.create_task(inner())
    scheduled_call_tasks.setdefault(guild_id, {})[call_id] = task


def schedule_reminder(guild_id: str, reminder_id: str, reminder: dict):
    async def inner():
        try:
            dt = datetime.fromisoformat(reminder['datetime'])
            now = datetime.now()
            if dt <= now:
                return

            delay = (dt - now).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

            channel = bot.get_channel(reminder['channel_id'])
            if not channel:
                return

            await channel.send(
                f"🔔 **Reminder!** 🔔\n"
                f"**{reminder['title']}**\n"
                f"||@everyone||"
            )

            # Clean up after firing
            if guild_id in scheduled_reminder_tasks and reminder_id in scheduled_reminder_tasks[guild_id]:
                scheduled_reminder_tasks[guild_id].pop(reminder_id, None)

            # Delete the reminder from the database
            await db_delete_reminder(guild_id, reminder_id)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Reminder error {reminder_id}: {e}")
        finally:
            scheduled_reminder_tasks.get(guild_id, {}).pop(reminder_id, None)

            task = asyncio.create_task(inner())
            scheduled_reminder_tasks.setdefault(guild_id, {})[reminder_id] = task
        
        # ────────────────────────────────────────────────
        # TODO MODAL# ────────────────────────────────────────────────

class TodoCreateModal(ui.Modal, title="Add New Todo Task"):
    task_description = ui.TextInput(
        label="Task / Todo",
        placeholder="e.g. Submit math assignment, Revise OS unit 3, Call mom...",
        required=True,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        task_text = self.task_description.value.strip()
        if not task_text:
            await interaction.response.send_message("Task cannot be empty.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        task_id = str(uuid.uuid4())
        created_by = str(interaction.user.id)
        created_at = datetime.now()

        success = await db_add_todo(guild_id, task_id, task_text, created_by, created_at)

        if not success:
            await interaction.response.send_message("Failed to add todo. Please try again.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ Todo added: **{task_text}**",
            ephemeral=False
        )

class UserInfoModal(ui.Modal, title="Tell me about yourself!"):
    user_name = ui.TextInput(label="Your Name", placeholder="e.g. Sidharth", required=True)
    user_nickname = ui.TextInput(label="Your Nickname", placeholder="e.g. Sid", required=True)
    user_age = ui.TextInput(label="Your Age", placeholder="e.g. 25", required=True)
    user_mood = ui.TextInput(label="How are you feeling today?", placeholder="e.g. Happy, stressed, curious", required=False)
    user_about_you = ui.TextInput(label="About You (hobbies, challenges)", style=discord.TextStyle.paragraph, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        username = self.user_name.value.strip()
        nickname = self.user_nickname.value.strip()
        age = self.user_age.value.strip()
        mood = self.user_mood.value.strip() if self.user_mood.value else "Not specified"
        about_you = self.user_about_you.value.strip() if self.user_about_you.value else "Not specified"

        try:
            age_int = int(age)
        except ValueError:
            await interaction.response.send_message("Please enter a valid age (a whole number).", ephemeral=True)
            return

        # Pass combined 'about_you' to both hobbies and challenges for now, or refine db_add_user_info
        success = await db_add_user_info(discord_id, username, nickname, age_int, mood, about_you, about_you)

        if success:
            await interaction.response.send_message(
                f"Hello {username} (aka {nickname})! I've noted that you're {age} years old and feeling {mood}. "
                "It's good to meet you! You can now use `/talk` to chat with me.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "There was an error saving your information. Please try again later.",
                ephemeral=True
            )

# ────────────────────────────────────────────────
# TODO SELECT VIEW (for removal)
# ────────────────────────────────────────────────

class TodoSelectView(ui.View):
    def __init__(self, todo_list: list[tuple[str, dict]]):
        super().__init__(timeout=180.0)
        self.todo_list = todo_list

        options = []
        for tid, data in todo_list:
            label = data["text"][:80]
            if len(data["text"]) > 80:
                label += "..."
            options.append(SelectOption(
                label=label,
                value=tid,
                description=f"Added by <@{data['created_by']}>"
            ))

        if not options:
            self.clear_items()
            return

        self.select = ui.Select(
            placeholder="Select task to MARK AS DONE / REMOVE...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.select.values[0]
        guild_id = str(interaction.guild_id)

        # Retrieve the task text before deleting for the response message
        todos = await db_get_todos(guild_id)
        task_text = ""
        for tid, data in self.todo_list:
            if tid == selected_id:
                task_text = data["text"]
                break
        
        success = await db_delete_todo(guild_id, selected_id)

        if not success:
            await interaction.response.send_message("Failed to delete task. Please try again.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"🗑️ Task completed / removed:\n**{task_text}**",
            view=None
        )


# ────────────────────────────────────────────────
# EVENT VIEWS
# ────────────────────────────────────────────────

class EventScheduleView(ui.View):
    def __init__(self, event_id: str, title: str):
        super().__init__(timeout=600.0)
        self.event_id = event_id
        self.title = title
        self.selected_date = None
        self.selected_hour = None
        self.selected_minute = None

        now = datetime.now()
        date_options = []
        for i in range(7):
            d = now + timedelta(days=i)
            label = d.strftime("%Y-%m-%d (%A)")
            date_options.append(SelectOption(
                label=label, value=d.strftime("%Y-%m-%d")))

        self.date_select = ui.Select(
            placeholder="Select date",
            options=date_options,
            min_values=1,
            max_values=1
        )
        self.date_select.callback = self.date_callback
        self.add_item(self.date_select)

        hour_options = [SelectOption(
            label=f"{h:02d}", value=f"{h:02d}") for h in range(24)]
        self.hour_select = ui.Select(
            placeholder="Hour (00-23)",
            options=hour_options,
            min_values=1,
            max_values=1
        )
        self.hour_select.callback = self.time_callback
        self.add_item(self.hour_select)

        minute_options = [SelectOption(label=m, value=m) for m in [
            "00", "10", "20", "30", "40", "50"]]
        self.minute_select = ui.Select(
            placeholder="Minute",
            options=minute_options,
            min_values=1,
            max_values=1
        )
        self.minute_select.callback = self.time_callback
        self.add_item(self.minute_select)

        self.confirm_button = ui.Button(
            label="Confirm & Schedule",
            style=discord.ButtonStyle.green,
            disabled=True
        )
        self.confirm_button.callback = self.confirm_callback
        self.add_item(self.confirm_button)

    async def date_callback(self, interaction: discord.Interaction):
        self.selected_date = self.date_select.values[0]
        self._update_button()
        await interaction.response.edit_message(view=self)

    async def time_callback(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == self.hour_select.custom_id:
            self.selected_hour = self.hour_select.values[0]
        else:
            self.selected_minute = self.minute_select.values[0]
        self._update_button()
        await interaction.response.edit_message(view=self)

    def _update_button(self):
        self.confirm_button.disabled = not (
            self.selected_date and self.selected_hour and self.selected_minute)

    async def confirm_callback(self, interaction: discord.Interaction):
        if not (self.selected_date and self.selected_hour and self.selected_minute):
            return

        dt_str = f"{self.selected_date} {self.selected_hour}:{self.selected_minute}:00"
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            if dt <= datetime.now():
                await interaction.response.send_message("Event must be in the future.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("Invalid date/time.", ephemeral=True)
            return

        # Use db_update_event_datetime to update the event in the database
        success = await db_update_event_datetime(
            str(interaction.guild_id),
            self.event_id,
            dt,
            interaction.channel_id
        )

        if not success:
            await interaction.response.send_message("Failed to schedule event. Please try again.", ephemeral=True)
            return

        # Fetch the updated event data to pass to schedule_spam
        events_data = await db_get_events(str(interaction.guild_id))
        event = next((e for e in events_data if e["event_id"] == self.event_id), None)

        if event:
            schedule_spam(str(interaction.guild_id), self.event_id, event)
        else:
            await interaction.response.send_message("Event not found after update, scheduling failed.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"**{self.title}** scheduled for **{dt.strftime('%Y-%m-%d %I:%M %p')}**",
            view=None
        )


class EventSelectView(ui.View):
    def __init__(self, events_list: list[tuple[str, dict]], action: str):
        super().__init__(timeout=180.0)
        self.events_list = events_list
        self.action = action

        options = []
        for eid, data in events_list:
            label = f"{data['title']} ({eid[:8]})"
            if 'datetime' in data and data['datetime']:
                try:
                    dt = datetime.fromisoformat(data['datetime'])
                    label += f" - {dt.strftime('%Y-%m-%d %H:%M')}"
                except:
                    pass
            options.append(SelectOption(label=label[:100], value=eid))

        self.select = ui.Select(
            placeholder=f"Select event to {action.replace('_', ' ')}...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.select.values[0]
        guild_id = str(interaction.guild_id)

        event = next((e for eid, e in self.events_list if eid == selected_id), None)

        if not event:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return

        title = event['title']

        if self.action == "delete":
            success = await db_delete_event(guild_id, selected_id)
            if not success:
                await interaction.response.send_message("Failed to delete event. Please try again.", ephemeral=True)
                return

            if guild_id in scheduled_tasks and selected_id in scheduled_tasks[guild_id]:
                scheduled_tasks[guild_id][selected_id].cancel()
                del scheduled_tasks[guild_id][selected_id]

            await interaction.response.send_message(f"Deleted event: **{title}**", ephemeral=True)

        elif self.action == "edit":
            await interaction.response.send_message(
                f"Selected for edit: **{title}** (ID: {selected_id[:8]})\n"
                f"Members: {', '.join(f'<@{mid}>' for mid in event['members'])}\n"
                f"Scheduled: {datetime.fromisoformat(event['datetime']).strftime('%Y-%m-%d %I:%M %p') if event['datetime'] else 'Not scheduled'}\n"
                "(Full edit functionality can be added later)",
                ephemeral=True
            )


# ────────────────────────────────────────────────
# REMINDER VIEWS
# ────────────────────────────────────────────────

class ReminderDateView(ui.View):
    def __init__(self, reminder_id: str, title: str):
        super().__init__(timeout=600.0)
        self.reminder_id = reminder_id
        self.title = title
        self.selected_date = None

        now = datetime.now()
        date_options = []
        for i in range(10):
            d = now + timedelta(days=i)
            label = d.strftime("%Y-%m-%d (%A)")
            date_options.append(SelectOption(
                label=label,
                value=d.strftime("%Y-%m-%d")
            ))

        self.date_select = ui.Select(
            placeholder="Select reminder date (fires at 8:00 PM IST)",
            options=date_options,
            min_values=1,
            max_values=1
        )
        self.date_select.callback = self.date_callback
        self.add_item(self.date_select)

        self.confirm_button = ui.Button(
            label="Confirm & Schedule",
            style=discord.ButtonStyle.green,
            disabled=True
        )
        self.confirm_button.callback = self.confirm_callback
        self.add_item(self.confirm_button)

    async def date_callback(self, interaction: discord.Interaction):
        self.selected_date = self.date_select.values[0]
        self.confirm_button.disabled = False
        await interaction.response.edit_message(view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
        if not self.selected_date:
            return

        dt_str = f"{self.selected_date} 20:00:00"
        try:
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            if dt <= datetime.now():
                await interaction.response.send_message("Selected date is in the past.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("Invalid date.", ephemeral=True)
            return

        # Use db_update_reminder_datetime to update the reminder in the database
        success = await db_update_reminder_datetime(
            str(interaction.guild_id),
            self.reminder_id,
            dt,
            interaction.channel_id
        )

        if not success:
            await interaction.response.send_message("Failed to schedule reminder. Please try again.", ephemeral=True)
            return

        # Fetch the updated reminder data to pass to schedule_reminder
        reminders_data = await db_get_reminders(str(interaction.guild_id))
        reminder = next((r for r in reminders_data if r["reminder_id"] == self.reminder_id), None)

        if reminder:
            schedule_reminder(str(interaction.guild_id), self.reminder_id, reminder)
        else:
            await interaction.response.send_message("Reminder not found after update, scheduling failed.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"Reminder '**{self.title}**' scheduled for **{dt.strftime('%Y-%m-%d %I:%M %p')}**",
            view=None
        )


class ReminderSelectView(ui.View):
    def __init__(self, reminders_list: list[tuple[str, dict]], action: str):
        super().__init__(timeout=180.0)
        self.reminders_list = reminders_list
        self.action = action

        options = []
        for rid, data in reminders_list:
            label = f"{data['title']}"
            if 'datetime' in data and data['datetime']:
                try:
                    dt = datetime.fromisoformat(data['datetime'])
                    label += f" - {dt.strftime('%Y-%m-%d %I:%M %p')}"
                except:
                    pass
            options.append(SelectOption(label=label[:100], value=rid))

        if not options:
            self.clear_items()
            return

        self.select = ui.Select(
            placeholder=f"Select reminder to {action.replace('_', ' ')}...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.select.values[0]
        reminders = await load_json(REMINDERS_FILE)
        guild_id = str(interaction.guild_id)

        if guild_id not in reminders or selected_id not in reminders[guild_id]:
            await interaction.response.send_message("Reminder not found.", ephemeral=True)
            return

        reminder = reminders[guild_id][selected_id]
        title = reminder['title']

        if self.action == "delete":
            success = await db_delete_reminder(guild_id, selected_id)
            if not success:
                await interaction.response.send_message("Failed to delete reminder. Please try again.", ephemeral=True)
                return

            if guild_id in scheduled_reminder_tasks and selected_id in scheduled_reminder_tasks[guild_id]:
                scheduled_reminder_tasks[guild_id][selected_id].cancel()
                del scheduled_reminder_tasks[guild_id][selected_id]

            await interaction.response.edit_message(
                content=f"Deleted reminder: **{title}**",
                view=None
            )

        elif self.action == "edit":
            reminders = await db_get_reminders(guild_id)
            reminder = next((r for r in reminders if r["reminder_id"] == selected_id), None)
            if reminder:
                await interaction.response.send_message(
                    f"Selected for edit: **{reminder['title']}**\n"
                    f"Current time: {reminder['datetime'] or 'Not scheduled'}\n"
                    "(Full edit coming soon – currently only view supported)",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message("Reminder not found.", ephemeral=True)


# ────────────────────────────────────────────────
# ASSIGNMENT SELECT VIEW
# ────────────────────────────────────────────────

class AssignmentSelectView(ui.View):
    def __init__(self, assignments_list: list[tuple[str, dict]]):
        super().__init__(timeout=180.0)
        self.assignments_list = assignments_list

        options = []
        for aid, data in assignments_list:
            label = f"{data['subject']} - {data['title']} - {data['deadline']}"
            options.append(SelectOption(label=label[:100], value=aid))

        self.select = ui.Select(
            placeholder="Select assignment...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.select.values[0]
        guild_id = str(interaction.guild_id)

        assignments_data = await db_get_assignments(guild_id)
        assign = next((a for a in assignments_data if a["assignment_id"] == selected_id), None)

        if not assign:
            await interaction.response.send_message("Assignment not found.", ephemeral=True)
            return

        file_paths = assign.get('file_paths', [])

        files = []
        for path in file_paths:
            if Path(path).exists():
                files.append(discord.File(path, filename=Path(path).name))

        embed = discord.Embed(
            title=assign['title'],
            description=assign.get('description', "No description"),
            color=0x2f3136
        )
        embed.add_field(name="Subject", value=assign['subject'], inline=True)
        embed.add_field(name="Deadline", value=assign['deadline'], inline=True)
        embed.add_field(
            name="Files", value=f"{len(file_paths)} file(s)", inline=True)

        await interaction.response.send_message(embed=embed, files=files)


# ────────────────────────────────────────────────
# NOTES SELECT VIEWS
# ────────────────────────────────────────────────

class SubjectSelectView(ui.View):
    def __init__(self, subjects: list[str]):
        super().__init__(timeout=180.0)
        options = [SelectOption(label=s, value=s) for s in subjects]
        self.select = ui.Select(
            placeholder="Select subject...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        subject = self.select.values[0]
        guild_id = str(interaction.guild_id)
        notes_data = await db_get_notes(guild_id)

        subject_notes = [
            (n["note_id"], n) for n in notes_data
            if n['subject'] == subject
        ]

        if not subject_notes:
            await interaction.response.send_message(
                f"No notes in **{subject}**.", ephemeral=True
            )
            return

        view = NoteSelectView(subject_notes)

        await interaction.response.edit_message(
            content=f"Select note from **{subject}**:",
            view=view
        )


class NoteSelectView(ui.View):
    def __init__(self, notes_list: list[tuple[str, dict]]):
        super().__init__(timeout=180.0)
        self.notes_list = notes_list
        options = [SelectOption(label=data['title'], value=nid)
                   for nid, data in notes_list]
        self.select = ui.Select(
            placeholder="Select note...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.select.values[0]
        guild_id = str(interaction.guild_id)

        note = next((n for nid, n in self.notes_list if nid == selected_id), None)

        if not note:
            await interaction.response.send_message("Note not found.", ephemeral=True)
            return

        file_paths = note.get('file_paths', [])

        files = [discord.File(path, filename=Path(path).name)
                 for path in file_paths if Path(path).exists()]

        embed = discord.Embed(
            title=note['title'],
            color=0x2f3136
        )
        embed.add_field(name="Subject", value=note['subject'], inline=True)
        embed.add_field(
            name="Files", value=f"{len(file_paths)} file(s)", inline=True)

        await interaction.message.edit(view=None)
        await interaction.response.send_message(embed=embed, files=files)


# ────────────────────────────────────────────────
# MODALS & VIEWS FOR NOTES & ASSIGNMENTS
# ────────────────────────────────────────────────

class NoteCreateModal(ui.Modal, title="Create New Note"):
    note_title = ui.TextInput(
        label="Title", placeholder="e.g. Math Notes Chapter 1", required=True)
    note_subject = ui.TextInput(
        label="Subject", placeholder="e.g. Math", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        title = self.note_title.value.strip()
        subject = self.note_subject.value.strip()

        guild_id = str(interaction.guild_id)
        note_id = str(uuid.uuid4())
        creator_id = str(interaction.user.id)

        success = await db_add_note(guild_id, note_id, title, subject, [], creator_id)

        if not success:
            await interaction.response.send_message("Failed to create note. Please try again.", ephemeral=True)
            return

        await interaction.response.send_message(f"Note '**{title}**' created under **{subject}**.", ephemeral=False)


class NoteAssignView(ui.View):
    def __init__(self, notes_list: list[tuple[str, dict]], temp_paths: list[str]):
        super().__init__(timeout=180.0)
        self.temp_paths = temp_paths

        options = []
        for nid, data in notes_list:
            label = f"{data['subject']} - {data['title']} ({nid[:8]})"
            options.append(SelectOption(label=label[:100], value=nid))

        self.select = ui.Select(
            placeholder="Select note to assign files...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.select.values[0]
        guild_id = str(interaction.guild_id)

        notes_data = await db_get_notes(guild_id)
        note = next((n for n in notes_data if n["note_id"] == selected_id), None)

        if not note:
            await interaction.response.send_message("Note not found.", ephemeral=True)
            return

        subject = note['subject']
        assets_dir = Path("assets/notes") / subject.replace(" ", "_")
        assets_dir.mkdir(parents=True, exist_ok=True)
        new_paths = []

        for temp_path_str in self.temp_paths:
            temp_path = Path(temp_path_str)
            if temp_path.exists():
                new_path = assets_dir / temp_path.name
                temp_path.rename(new_path)
                new_paths.append(str(new_path))

        updated_file_paths = note['file_paths'] + new_paths
        success = await db_update_note_files(guild_id, selected_id, updated_file_paths)

        if not success:
            await interaction.response.send_message("Failed to add files to note. Please try again.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"Added {len(new_paths)} file(s) to note '**{note['title']}**'.",
            view=None
        )


class AssignmentCreateModal(ui.Modal, title="Create New Assignment"):
    title_input = ui.TextInput(
        label="Title", placeholder="e.g. Math Homework #3", required=True)
    description_input = ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph, required=False)
    deadline_input = ui.TextInput(
        label="Deadline (YYYY-MM-DD HH:MM)", placeholder="2025-12-31 23:59", required=True)
    subject_input = ui.TextInput(
        label="Subject", placeholder="e.g. Math", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        title = self.title_input.value.strip()
        description = self.description_input.value.strip()
        deadline = self.deadline_input.value.strip()
        subject = self.subject_input.value.strip()

        try:
            dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M")
            if dt <= datetime.now():
                await interaction.response.send_message("Deadline must be in the future.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("Invalid deadline format. Use YYYY-MM-DD HH:MM.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        assignment_id = str(uuid.uuid4())
        creator_id = str(interaction.user.id)

        success = await db_add_assignment(guild_id, assignment_id, title, description, dt, subject, [], creator_id)

        if not success:
            await interaction.response.send_message("Failed to create assignment. Please try again.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Assignment '**{title}**' created under **{subject}** with deadline {dt.strftime('%Y-%m-%d %H:%M')}.",
            ephemeral=False
        )


class AssignmentAssignView(ui.View):
    def __init__(self, assignments_list: list[tuple[str, dict]], temp_paths: list[str]):
        super().__init__(timeout=180.0)
        self.temp_paths = temp_paths

        options = []
        for aid, data in assignments_list:
            label = f"{data['subject']} - {data['title']} ({aid[:8]}) - Due {data['deadline']}"
            options.append(SelectOption(label=label[:100], value=aid))

        self.select = ui.Select(
            placeholder="Select assignment to assign files...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.callback
        self.add_item(self.select)

    async def callback(self, interaction: discord.Interaction):
        selected_id = self.select.values[0]
        guild_id = str(interaction.guild_id)

        assignments_data = await db_get_assignments(guild_id)
        assign = next((a for a in assignments_data if a["assignment_id"] == selected_id), None)

        if not assign:
            await interaction.response.send_message("Assignment not found.", ephemeral=True)
            return

        subject = assign['subject']
        assets_dir = Path("assets/assignments") / subject.replace(" ", "_")
        assets_dir.mkdir(parents=True, exist_ok=True)
        new_paths = []

        for temp_path_str in self.temp_paths:
            temp_path = Path(temp_path_str)
            if temp_path.exists():
                new_path = assets_dir / temp_path.name
                temp_path.rename(new_path)
                new_paths.append(str(new_path))

        updated_file_paths = assign['file_paths'] + new_paths
        success = await db_update_assignment_files(guild_id, selected_id, updated_file_paths)

        if not success:
            await interaction.response.send_message("Failed to add files to assignment. Please try again.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"Added {len(new_paths)} file(s) to assignment '**{assign['title']}**'.",
            view=None
        )


# ────────────────────────────────────────────────
# HELP COMMAND (updated to include new reminder commands)
# ────────────────────────────────────────────────

@tree.command(name="help", description="Show all available commands and their usage")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="JOI Bot Commands Help",
        description="Here are all the commands you can use:",
        color=0x5865F2
    )

    commands_list = [
        ("**/help**", "Shows this help message"),
        ("**/todo**", "Add a new task to the guild todo list"),
        ("**/todo-list**", "View and manage (complete/remove) guild todo tasks"),
        ("**/set-reminder**", "Create a group reminder that pings @everyone"),
        ("**/delete-reminder**", "Delete an existing group reminder"),
        ("**/edit-reminder**", "Edit an existing group reminder (basic)"),
        ("**/create-notes**", "Create a new note entry"),
        ("**/load-notes**", "Upload files → assign them to an existing note"),
        ("**/fetch-notes**", "Browse and download study notes by subject"),
        ("**/create-assignment**", "Create a new assignment entry"),
        ("**/load-assignment**", "Upload files → assign to an existing assignment"),
        ("**/fetch-assignments**", "View and download assignments with files"),
        ("**/talk**", "Talk to JOI (Gemini AI)"),
        ("**/set-event**", "Create a new event with mentioned members"),
        ("**/delete-event**", "Delete an existing event"),
        ("**/edit-event**", "Select an event to edit (placeholder)"),
        ("**/stop-reminder**", "Remove yourself from active event reminders"),
        ("**/call**", "Schedule mass pings (call spam) after delay"),
        ("**/stop-calling**", "Remove yourself from active call spam"),
        ("**/check-attendance**", "Show your attendance stats from Firebase"),
        ("**/timetable**", "Display your timetable image"),
        ("**/soonambedu**", "Get a random image from the Soonambedu collection"),
        ("**/diddyfrancis**", "Get a random Shyam Francis related image"),
        ("**/add-lab-manual**", "Create a new lab manual subject folder"),
        ("**/fetch-lab-manual-programs**",
         "Browse and read lab experiment code files"),
    ]

    for name, desc in commands_list:
        embed.add_field(name=name, value=desc, inline=False)

    embed.set_footer(
        text="JOI - EVERYTHING YOU WANT TO SEE, EVERYTHING YOU WANT TO HEAR")

    await interaction.response.send_message(embed=embed, ephemeral=False)

# ────────────────────────────────────────────────
# UPTIME CLI (shows real server uptime)
# ────────────────────────────────────────────────


@tree.command(name="uptime-cli", description="Show how long the server has been running (real host uptime)")
async def cmd_uptime_cli(interaction: discord.Interaction):
    # Optional: restrict to admin / specific users if desired
    # if interaction.user.name.lower() != "sidhartheverett":
    #     await interaction.response.send_message("This command is restricted.", ephemeral=True)
    #     return

    # Give us time to run system command
    await interaction.response.defer(ephemeral=False)

    try:
        # Run the actual uptime command
        import subprocess
        result = subprocess.run(
            ["uptime"],
            capture_output=True,
            text=True,
            timeout=8
        )

        if result.returncode != 0:
            await interaction.followup.send(
                "Failed to read server uptime.\n"
                f"Error: {result.stderr.strip() or 'command failed'}",
                ephemeral=True
            )
            return

        raw_output = result.stdout.strip()

        # ────────────────────────────────────────────────
        # Parse typical uptime output (Linux)
        # Examples:
        #  15:42:19 up  5 days,  3:17,  1 user,  load average: 0.12, 0.15, 0.18
        #  10:05:22 up 12 min,  3 users,  load average: 1.45, 0.92, 0.68
        # ────────────────────────────────────────────────

        # Basic clean version
        embed = discord.Embed(
            title="🖥️ Server Uptime",
            color=0x00c4b4,
            timestamp=datetime.utcnow()
        )

        embed.add_field(
            name="Uptime Output",
            value=f"```\n{raw_output}\n```",
            inline=False
        )

        # Try to extract nicer fields (optional parsing)
        try:
            parts = raw_output.split("up", 1)
            if len(parts) == 2:
                time_part = parts[1].strip().split(",", 2)
                uptime_str = time_part[0].strip()
                users_load = ", ".join(time_part[1:]).strip() if len(
                    time_part) > 1 else ""

                embed.add_field(name="Running for",
                                value=uptime_str, inline=True)
                if users_load:
                    embed.add_field(name="Users / Load",
                                    value=users_load, inline=True)
        except:
            pass  # fallback to raw if parsing fails

        embed.set_footer(text="Host machine uptime • JOI Bot")

        await interaction.followup.send(embed=embed)

    except FileNotFoundError:
        await interaction.followup.send(
            "The `uptime` command is not available on this server.",
            ephemeral=True
        )
    except subprocess.TimeoutExpired:
        await interaction.followup.send(
            "Reading uptime timed out.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"Error while checking uptime:\n```py\n{type(e).__name__}: {str(e)}\n```",
            ephemeral=True
        )

# ────────────────────────────────────────────────
# ADMIN ONLY COMMANDS
# ────────────────────────────────────────────────


@tree.command(name="admin-announce", description="Send server-wide announcement (admin only)")
@app_commands.describe(
    message="The announcement text to send with @everyone"
)
async def admin_announce(interaction: discord.Interaction, message: str):
    if interaction.user.name.lower() != "sidhartheverett":
        await interaction.response.send_message(
            "⛔ This command is restricted to **sidhartheverett** only.",
            ephemeral=True
        )
        return

    announcement = message.strip()
    if not announcement:
        await interaction.response.send_message("Cannot send empty announcement.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await interaction.channel.send(
            f"📢 **OFFICIAL ANNOUNCEMENT** 📢\n\n{announcement}\n\n||@everyone||"
        )
        await interaction.followup.send(
            "Announcement posted successfully ✓",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)

# ────────────────────────────────────────────────
# LAB MANUAL COMMANDS
# ────────────────────────────────────────────────


@tree.command(name="add-lab-manual", description="Create a new lab manual subject folder")
@app_commands.describe(subject="Name of the lab/subject (e.g. Data Structures Lab)")
async def cmd_add_lab_manual(interaction: discord.Interaction, subject: str):
    folder_name = re.sub(r'[^a-zA-Z0-9_-]', '_', subject.strip())
    folder_path = LAB_MANUALS_DIR / folder_name

    if folder_path.exists():
        await interaction.response.send_message(
            f"Lab manual for **{subject}** (`{folder_name}`) already exists.", ephemeral=True
        )
        return

    try:
        folder_path.mkdir(parents=True, exist_ok=True)
        await interaction.response.send_message(
            f"Lab manual subject **{subject}** created.\n"
            f"Folder: `lab-manuals/{folder_name}/`\n"
            "Add experiment files manually (e.g. `exp1.txt`, `exp2.txt`, etc.)",
            ephemeral=False
        )
    except Exception as e:
        await interaction.response.send_message(f"Failed to create folder: {str(e)}", ephemeral=True)


@tree.command(name="fetch-lab-manual-programs", description="Browse and get lab manual experiment code")
async def cmd_fetch_lab_manual(interaction: discord.Interaction):
    if not LAB_MANUALS_DIR.exists() or not any(LAB_MANUALS_DIR.iterdir()):
        await interaction.response.send_message(
            "No lab manuals found. Create one with `/add-lab-manual`.",
            ephemeral=True
        )
        return

    subjects = [d.name.replace('_', ' ')
                for d in LAB_MANUALS_DIR.iterdir() if d.is_dir()]
    subjects.sort()

    if not subjects:
        await interaction.response.send_message("No lab manual subjects found.", ephemeral=True)
        return

    class SubjectDropdown(ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            subject_select = ui.Select(
                placeholder="Select Lab Manual Subject...",
                options=[SelectOption(label=s, value=s.replace(' ', '_'))
                         for s in subjects]
            )
            subject_select.callback = self.on_subject_select
            self.add_item(subject_select)

        async def on_subject_select(self, inter: discord.Interaction):
            folder_name = inter.data["values"][0]
            subject_path = LAB_MANUALS_DIR / folder_name

            experiments = []
            for file in subject_path.glob("exp*.txt"):
                try:
                    num = int(file.stem.replace("exp", ""))
                    experiments.append((num, file.name, str(file)))
                except ValueError:
                    continue

            if not experiments:
                await inter.response.send_message(
                    f"No experiment files (`exp*.txt`) found in **{folder_name.replace('_', ' ')}**.",
                    ephemeral=True
                )
                return

            experiments.sort(key=lambda x: x[0])

            class ExperimentDropdown(ui.View):
                def __init__(self):
                    super().__init__(timeout=180)
                    exp_select = ui.Select(
                        placeholder="Select Experiment...",
                        options=[SelectOption(
                            label=f"Experiment {num} - {fname}",
                            value=file_path
                        ) for num, fname, file_path in experiments]
                    )
                    exp_select.callback = self.on_exp_select
                    self.add_item(exp_select)

                async def on_exp_select(self, inter2: discord.Interaction):
                    file_path = Path(inter2.data["values"][0])
                    if not file_path.exists():
                        await inter2.response.send_message("File disappeared.", ephemeral=True)
                        return

                    try:
                        content = file_path.read_text(encoding="utf-8")
                        if len(content) > 1900:
                            content = content[:1900] + \
                                "\n\n... (truncated - full file on server)"
                        await inter2.response.send_message(
                            f"**Experiment from {folder_name.replace('_', ' ')}**\n"
                            f"```{file_path.name}```\n```txt\n{content}\n```",
                            ephemeral=False
                        )
                    except Exception as e:
                        await inter2.response.send_message(f"Error reading file: {str(e)}", ephemeral=True)

            await inter.response.edit_message(
                content=f"Select experiment from **{folder_name.replace('_', ' ')}**:",
                view=ExperimentDropdown()
            )

    view = SubjectDropdown()
    await interaction.response.send_message("Select lab manual subject:", view=view, ephemeral=False)


# ────────────────────────────────────────────────
# REMINDER COMMANDS
# ────────────────────────────────────────────────

@tree.command(name="set-reminder", description="Create a new reminder for the group (pings @everyone)")
@app_commands.describe(title="Reminder title / message")
async def cmd_set_reminder(interaction: discord.Interaction, title: str):
    guild_id = str(interaction.guild_id)
    reminder_id = str(uuid.uuid4())
    creator_id = str(interaction.user.id)

    # Initially create the reminder with datetime and channel_id as None, they will be updated by ReminderDateView
    success = await db_add_reminder(guild_id, reminder_id, title.strip(), creator_id, None, None)

    if not success:
        await interaction.response.send_message("Failed to create reminder. Please try again.", ephemeral=True)
        return

    view = ReminderDateView(reminder_id, title)

    await interaction.response.send_message(
        f"Reminder **{title}** created.\n"
        "Please select a date below (will fire at 8:00 PM IST):",
        view=view,
        ephemeral=False
    )


@tree.command(name="delete-reminder", description="Delete an existing group reminder")
async def cmd_delete_reminder(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    reminders = await db_get_reminders(guild_id)

    if not reminders:
        await interaction.response.send_message("No reminders found in this server.", ephemeral=True)
        return

    # Convert the list of dictionaries to a list of tuples (reminder_id, data)
    reminder_list = [(r["reminder_id"], r) for r in reminders]
    view = ReminderSelectView(reminder_list, action="delete")

    if not view.children:
        await interaction.response.send_message("No reminders available to delete.", ephemeral=True)
        return

    await interaction.response.send_message(
        "Select the reminder you want to **delete**:",
        view=view,
        ephemeral=True
    )


@tree.command(name="edit-reminder", description="Edit an existing group reminder (title/date)")
async def cmd_edit_reminder(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    reminders = await db_get_reminders(guild_id)

    if not reminders:
        await interaction.response.send_message("No reminders found in this server.", ephemeral=True)
        return

    reminder_list = [(r["reminder_id"], r) for r in reminders]
    view = ReminderSelectView(reminder_list, action="edit")

    if not view.children:
        await interaction.response.send_message("No reminders available to edit.", ephemeral=True)
        return

    await interaction.response.send_message(
        "Select the reminder you want to **edit**:",
        view=view,
        ephemeral=True
    )


# ────────────────────────────────────────────────
# TODO COMMANDS
# ────────────────────────────────────────────────

@tree.command(name="todo", description="Add a new task to the guild's todo list")
async def cmd_todo(interaction: discord.Interaction):
    await interaction.response.send_modal(TodoCreateModal())


@tree.command(name="todo-list", description="Show and manage the guild's todo list")
async def cmd_todo_list(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    todos = await db_get_todos(guild_id)

    if not todos:
        await interaction.response.send_message(
            "🎉 The guild to-do list is currently empty!\nAdd something with `/todo`",
            ephemeral=False
        )
        return

    todo_items = [(t["task_id"], t) for t in todos]

    # Build embed
    embed = discord.Embed(
        title="📋 Guild To-Do List",
        color=0x5865F2,
        description="Current pending tasks for the server"
    )

    for i, (task_id, data) in enumerate(todo_items, 1):
        created_by = f"<@{data['created_by']}>"
        created_at = datetime.fromisoformat(
            data['created_at']).strftime("%b %d, %Y %H:%M")
        value = f"Added by {created_by} • {created_at}"
        embed.add_field(
            name=f"{i}. {data['text']}",
            value=value,
            inline=False
        )

    embed.set_footer(
        text=f"{len(todo_items)} task{'s' if len(todo_items) != 1 else ''} • Use the menu below to complete tasks")

    view = TodoSelectView(todo_items)

    if not view.children:
        embed.description = "No tasks available to manage right now."
        view = None

    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=False
    )

# ────────────────────────────────────────────────
# WEB SERVER
# ────────────────────────────────────────────────


async def handle_assignment_upload(request):
    if not DEFAULT_GUILD_ID:
        return web.json_response({'status': 'error', 'message': 'Server not configured'}, status=500)

    guild_id = DEFAULT_GUILD_ID

    temp_dir = Path("assets/assignments/temp")
    temp_dir.mkdir(parents=True, exist_ok=True)

    reader = await request.multipart()
    data = {}
    temp_paths = []

    part = await reader.next()
    while part is not None:
        fieldname = part.name
        if fieldname == 'files':
            filename = part.filename
            if filename:
                temp_path = temp_dir / filename
                with open(temp_path, 'wb') as f:
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        f.write(chunk)
                temp_paths.append(str(temp_path))
        else:
            value = await part.read(decode=True)
            data[fieldname] = value.decode('utf-8')
        part = await reader.next()

    if not all(key in data for key in ['title', 'description', 'deadline', 'subject']):
        return web.json_response({'status': 'error', 'message': 'Missing fields'}, status=400)

    title = data['title']
    description = data['description']
    deadline = data['deadline']
    subject = data['subject']

    try:
        dt = datetime.fromisoformat(deadline.replace('T', ' ') + ':00')
        if dt <= datetime.now():
            return web.json_response({'status': 'error', 'message': 'Deadline must be in the future'}, status=400)
        deadline_str = dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return web.json_response({'status': 'error', 'message': 'Invalid deadline format'}, status=400)

    assignments = await load_json(ASSIGNMENTS_FILE)
    assignments.setdefault(guild_id, {})

    assignment_id = str(uuid.uuid4())
    assets_dir = Path("assets/assignments") / subject.replace(" ", "_")
    assets_dir.mkdir(parents=True, exist_ok=True)
    new_paths = []

    for temp_path_str in temp_paths:
        temp_path = Path(temp_path_str)
        new_path = assets_dir / temp_path.name
        temp_path.rename(new_path)
        new_paths.append(str(new_path))

    success = await db_add_assignment(guild_id, assignment_id, title, description, dt, subject, new_paths, 'web_upload')

    if not success:
        return web.json_response({'status': 'error', 'message': 'Failed to save assignment'}, status=500)

    return web.json_response({'status': 'success'})


async def handle_notes_upload(request):
    if not DEFAULT_GUILD_ID:
        return web.json_response({'status': 'error', 'message': 'Server not configured'}, status=500)

    guild_id = DEFAULT_GUILD_ID

    temp_dir = Path("assets/notes/temp")
    temp_dir.mkdir(parents=True, exist_ok=True)

    reader = await request.multipart()
    data = {}
    temp_paths = []

    part = await reader.next()
    while part is not None:
        fieldname = part.name
        if fieldname == 'files':
            filename = part.filename
            if filename:
                temp_path = temp_dir / filename
                with open(temp_path, 'wb') as f:
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        f.write(chunk)
                temp_paths.append(str(temp_path))
        else:
            value = await part.read(decode=True)
            data[fieldname] = value.decode('utf-8')
        part = await reader.next()

    if not all(key in data for key in ['title', 'subject']):
        return web.json_response({'status': 'error', 'message': 'Missing fields'}, status=400)

    title = data['title']
    subject = data['subject']

    note_id = str(uuid.uuid4())
    assets_dir = Path("assets/notes") / subject.replace(" ", "_")
    assets_dir.mkdir(parents=True, exist_ok=True)
    new_paths = []

    for temp_path_str in temp_paths:
        temp_path = Path(temp_path_str)
        new_path = assets_dir / temp_path.name
        temp_path.rename(new_path)
        new_paths.append(str(new_path))

    success = await db_add_note(guild_id, note_id, title, subject, new_paths, 'web_upload')

    if not success:
        return web.json_response({'status': 'error', 'message': 'Failed to save note'}, status=500)

    return web.json_response({'status': 'success'})


async def start_web():
    app = web.Application()
    app.router.add_static('/assignments', 'load-assignment')
    app.router.add_post('/assignments/upload', handle_assignment_upload)
    app.router.add_static('/notes', 'upload-notes')
    app.router.add_post('/notes/upload', handle_notes_upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
    await site.start()
    print("[WEB] Web server started at http://localhost:8080/assignments and http://localhost:8080/notes")


# ────────────────────────────────────────────────
# SLASH COMMANDS (continued)
# ────────────────────────────────────────────────

@tree.command(name="create-notes", description="Create a new note")
async def cmd_create_notes(interaction: discord.Interaction):
    await interaction.response.send_modal(NoteCreateModal())


@tree.command(name="load-notes", description="Upload files to an existing note")
async def cmd_load_notes(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    notes_data = await db_get_notes(guild_id)

    if not notes_data:
        await interaction.response.send_message(
            "No notes found. Create one first with `/create-notes`.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Please reply to **this message** with your file attachments.\n"
        "(You can attach multiple files in one message)",
        ephemeral=False
    )

    initial_msg = await interaction.original_response()

    def check(m: discord.Message):
        return (
            m.author.id == interaction.user.id
            and m.reference is not None
            and m.reference.message_id == initial_msg.id
        )

    try:
        msg = await bot.wait_for('message', check=check, timeout=300.0)
    except asyncio.TimeoutError:
        await interaction.followup.send("Upload timed out (5 minutes).", ephemeral=True)
        return

    if not msg.attachments:
        await interaction.followup.send("No files were attached in your reply.", ephemeral=True)
        return

    temp_dir = Path("assets/notes/temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_paths = []

    for att in msg.attachments:
        temp_path = temp_dir / att.filename
        await att.save(temp_path)
        temp_paths.append(str(temp_path))

    notes_list = [(n["note_id"], n) for n in notes_data]

    if not notes_list:
        for p in temp_paths:
            Path(p).unlink(missing_ok=True)
        await interaction.followup.send("No notes available to assign to.", ephemeral=True)
        return

    view = NoteAssignView(notes_list, temp_paths)

    await interaction.followup.send(
        f"Uploaded **{len(temp_paths)}** file(s).\n"
        "Select which note these files belong to:",
        view=view,
        ephemeral=False
    )


@tree.command(name="create-assignment", description="Create a new assignment")
async def cmd_create_assignment(interaction: discord.Interaction):
    await interaction.response.send_modal(AssignmentCreateModal())


@tree.command(name="load-assignment", description="Upload files to an existing assignment")
async def cmd_load_assignment(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    assignments = await db_get_assignments(guild_id)

    if not assignments:
        await interaction.response.send_message(
            "No assignments found. Create one first with `/create-assignment`.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Please reply to **this message** with your file attachments.\n"
        "(You can attach multiple files in one message)",
        ephemeral=False
    )

    initial_msg = await interaction.original_response()

    def check(m: discord.Message):
        return (
            m.author.id == interaction.user.id
            and m.reference is not None
            and m.reference.message_id == initial_msg.id
        )

    try:
        msg = await bot.wait_for('message', check=check, timeout=300.0)
    except asyncio.TimeoutError:
        await interaction.followup.send("Upload timed out (5 minutes).", ephemeral=True)
        return

    if not msg.attachments:
        await interaction.followup.send("No files were attached in your reply.", ephemeral=True)
        return

    temp_dir = Path("assets/assignments/temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_paths = []

    for att in msg.attachments:
        temp_path = temp_dir / att.filename
        await att.save(temp_path)
        temp_paths.append(str(temp_path))

    assign_list = [(a["assignment_id"], a) for a in assignments]

    if not assign_list:
        for p in temp_paths:
            Path(p).unlink(missing_ok=True)
        await interaction.followup.send("No assignments available to assign to.", ephemeral=True)
        return

    view = AssignmentAssignView(assign_list, temp_paths)

    await interaction.followup.send(
        f"Uploaded **{len(temp_paths)}** file(s).\n"
        "Select which assignment these files belong to:",
        view=view,
        ephemeral=False
    )


@tree.command(name="fetch-notes", description="List and fetch study notes")
async def cmd_fetch_notes(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    notes_data = await db_get_notes(guild_id)

    if not notes_data:
        await interaction.response.send_message("No notes found in this server.", ephemeral=True)
        return

    subjects = sorted(set(data['subject'] for data in notes_data))

    if not subjects:
        await interaction.response.send_message("No subjects with notes found.", ephemeral=True)
        return

    list_text = "**Available Subjects:**\n" + \
        "\n".join(f"• {s}" for s in subjects)

    view = SubjectSelectView(subjects)

    await interaction.response.send_message(
        list_text + "\n\nSelect a subject to view notes:",
        view=view,
        ephemeral=False
    )


@tree.command(name="set-event", description="Create a new event")
@app_commands.describe(
    title="Event title",
    members="Mention members with @ (space separated)"
)
async def cmd_set_event(interaction: discord.Interaction, title: str, members: str):
    member_ids = re.findall(r'<@!?(\d+)>', members)
    if not member_ids:
        await interaction.response.send_message("No valid members mentioned.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    event_id = str(uuid.uuid4())
    creator_id = str(interaction.user.id)

    success = await db_add_event(guild_id, event_id, title, member_ids, creator_id, None, None)

    if not success:
        await interaction.response.send_message("Failed to create event. Please try again.", ephemeral=True)
        return

    mentions_str = ' '.join(f'<@{mid}>' for mid in member_ids)

    view = EventScheduleView(event_id, title)

    await interaction.response.send_message(
        f"Event **{title}** created with members: {mentions_str}\n\n"
        "Please select date and time below:",
        view=view
    )


@tree.command(name="delete-event", description="Delete an existing event")
async def cmd_delete_event(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    events = await db_get_events(guild_id)

    if not events:
        await interaction.response.send_message("No events found in this server.", ephemeral=True)
        return

    event_list = [(e["event_id"], e) for e in events]
    view = EventSelectView(event_list, action="delete")
    await interaction.response.send_message(
        "Select the event you want to **delete**:",
        view=view,
        ephemeral=True
    )


@tree.command(name="edit-event", description="Edit an existing event (placeholder)")
async def cmd_edit_event(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    events = await db_get_events(guild_id)

    if not events:
        await interaction.response.send_message("No events found in this server.", ephemeral=True)
        return

    event_list = [(e["event_id"], e) for e in events]
    view = EventSelectView(event_list, action="edit")
    await interaction.response.send_message(
        "Select the event you want to **edit** (full edit coming soon):",
        view=view,
        ephemeral=True
    )


@tree.command(name="stop-reminder", description="Stop being reminded for your active events")
async def cmd_stop_reminder(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    user_id = str(interaction.user.id)

    if guild_id not in active_reminders or not active_reminders[guild_id]:
        await interaction.response.send_message("You are not currently being reminded for any events.", ephemeral=True)
        return

    stopped_titles = []
    fully_cleared_titles = []

    for event_id, data in list(active_reminders[guild_id].items()):
        if user_id in data['remaining']:
            data['remaining'].remove(user_id)
            stopped_titles.append(data['title'])

            if len(data['remaining']) == 0:
                fully_cleared_titles.append(data['title'])

                if guild_id in scheduled_tasks and event_id in scheduled_tasks[guild_id]:
                    scheduled_tasks[guild_id][event_id].cancel()
                    del scheduled_tasks[guild_id][event_id]

                del active_reminders[guild_id][event_id]

                success = await db_delete_event(guild_id, event_id)
                if not success:
                    print(f"Error deleting event {event_id} from database during stop-reminder.")

    if guild_id in active_reminders and not active_reminders[guild_id]:
        del active_reminders[guild_id]

    if not stopped_titles:
        await interaction.response.send_message("You weren't in any active reminder lists.", ephemeral=True)
        return

    reply = f"Stopped reminders for: **{', '.join(stopped_titles)}**"

    if fully_cleared_titles:
        reply += f"\n\n**All members have stopped reminders for:** {', '.join(fully_cleared_titles)}\n"
        reply += "These events have been fully cleared and deleted."

    await interaction.response.send_message(reply, ephemeral=False)


@tree.command(name="fetch-assignments", description="List and fetch assignments")
async def cmd_fetch_assignments(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    assignments = await db_get_assignments(guild_id)

    if not assignments:
        await interaction.response.send_message("No assignments found in this server.", ephemeral=True)
        return

    assign_list = [(a["assignment_id"], a) for a in assignments if a.get('subject')]

    if not assign_list:
        await interaction.response.send_message("No complete assignments found.", ephemeral=True)
        return

    list_text = "**Available Assignments:**\n"
    for _, data in assign_list:
        list_text += f"• {data['subject']} | {data['title']} | Deadline: {data['deadline']}\n"

    view = AssignmentSelectView(assign_list)

    await interaction.response.send_message(
        list_text + "\nSelect one to view/download files:",
        view=view,
        ephemeral=False
    )

@tree.command(name="talk", description="Talk to JOI (Gemini AI)")
@app_commands.describe(prompt="Your message to JOI")
async def cmd_talk(interaction: discord.Interaction, prompt: str):
    user_id = str(interaction.user.id)
    discord_username = interaction.user.name # Get Discord username

    # Check if user information exists
    user_info = await db_get_user_info(user_id)

    if user_info is None:
        # First-time user: present modal to gather info
        await interaction.response.send_modal(UserInfoModal())
        # The interaction will be responded to and handled by the modal's on_submit
        return

    # Defer the response for AI processing time
    await interaction.response.defer()

    try:
        # Construct dynamic system prompt with user info and mood
        dynamic_system_prompt = JOI_SYSTEM_PROMPT
        if user_info['mood'] and user_info['mood'] != "Not specified":
            dynamic_system_prompt += f"\nThe user, named {user_info['username']}, is currently feeling {user_info['mood']}."

        # Retrieve conversation history
        conversation_history = await db_get_conversation_history(user_id, limit=5) # Get last 5 turns
        
        # Prepare history for Gemini model
        history_for_gemini = []
        for conv_turn in conversation_history:
            history_for_gemini.append({"role": "user", "parts": [conv_turn["user_message"]]})
            # Ensure bot_response is not None before adding
            if conv_turn["bot_response"]:
                history_for_gemini.append({"role": "model", "parts": [conv_turn["bot_response"]]})

        model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=dynamic_system_prompt)
        chat = model.start_chat(history=history_for_gemini) # Initialize chat with history

        response = chat.send_message(prompt)

        response_text = response.text
        if len(response_text) > 2000:
            response_text = response_text[:1997] + "..."

        await interaction.followup.send(response_text)

        # Log the current conversation turn
        await db_add_conversation(user_id, prompt, response_text)

    except Exception as e:
        print(f"Error in /talk command for user {user_id}: {e}")
        await interaction.followup.send(
            "I'm sorry, I couldn't process that. Please try again later.",
            ephemeral=True
        )


@tree.command(name="timetable", description="Shows your timetable as an image")
async def cmd_timetable(interaction: discord.Interaction):
    await interaction.response.defer()

    image_path = "assets/timetable.jpeg"

    if not os.path.isfile(image_path):
        await interaction.followup.send(
            f"Timetable image not found at `{image_path}`.",
            ephemeral=True
        )
        return

    file = discord.File(image_path, filename="timetable.png")

    embed = discord.Embed(
        title=f"{interaction.user.display_name}'s Timetable",
        color=0x2f3136
    )
    embed.set_image(url="attachment://timetable.png")
    embed.set_footer(
        text="JOI - EVERYTHING YOU WANT TO SEE, EVERYTHING YOU WANT TO HEAR")

    await interaction.followup.send(embed=embed, file=file)


@tree.command(name="check-attendance", description="Check your attendance from Firebase")
async def cmd_check_attendance(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    username = interaction.user.name
    reg = await db_get_registration(username)

    if not reg:
        await interaction.followup.send("No registration number found for your username. Please use `/register <your_registration_number>` to register.")
        return

    student = next((s for s in students if s["reg"] == reg), None)
    if not student:
        await interaction.followup.send("Invalid registration number associated with your username.")
        return

    name = student["name"]

    loop = asyncio.get_running_loop()
    try:
        attendance = await loop.run_in_executor(None, get_attendance_data, reg)
    except Exception as e:
        await interaction.followup.send(f"Error fetching attendance: {str(e)}")
        return

    if attendance["perc"] < 75:
        color = 0xef4444  # red
    elif attendance["perc"] < 80:
        color = 0xf59e0b  # yellow
    else:
        color = 0x10b981  # green

    embed = discord.Embed(title=f"{name}'s Attendance Dashboard", color=color)
    embed.add_field(name="Registration Number", value=reg, inline=False)
    embed.add_field(name="Status", value=attendance["status"], inline=True)
    embed.add_field(name="Attendance Percentage",
                    value=f"{attendance['perc']:.2f}%", inline=True)
    embed.add_field(name="Total Days", value=str(
        attendance["total"]), inline=True)
    embed.add_field(name="Present Days", value=str(
        attendance["present"]), inline=True)
    embed.add_field(name="Absent Days", value=str(
        attendance["absent"]), inline=True)
    embed.add_field(name="On Duty Days", value=str(
        attendance["od"]), inline=True)
    embed.add_field(name="Safe Leave Days", value=str(
        attendance["safe_leave_days"]), inline=False)

    abs_str = "\n".join(attendance["abs_dates"]
                        ) if attendance["abs_dates"] else "None"
    if len(abs_str) > 1024:
        abs_str = f"Too many to list ({attendance['absent']} absent days). Check website for details."

    int_str = "\n".join(attendance["int_dates"]
                        ) if attendance["int_dates"] else "None"
    if len(int_str) > 1024:
        int_str = f"Too many to list."

    ext_str = "\n".join(attendance["ext_dates"]
                        ) if attendance["ext_dates"] else "None"
    if len(ext_str) > 1024:
        ext_str = f"Too many to list."

    embed.add_field(name="Absent Dates (DD-MM-YYYY)",
                    value=abs_str, inline=False)
    embed.add_field(name="Internal OD Dates (DD-MM-YYYY)",
                    value=int_str, inline=False)
    embed.add_field(name="External OD Dates (DD-MM-YYYY)",
                    value=ext_str, inline=False)

    embed.set_footer(
        text="Data fetched from Firebase. Legend: Red=Absent, Blue=Present, Yellow=OD, Purple=Holiday (not tracked here)")

    await interaction.followup.send(embed=embed)


# ────────────────────────────────────────────────
# NEW CALL COMMANDS
# ────────────────────────────────────────────────

@tree.command(name="call", description="Start calling mentioned members after a delay (spams every 2 seconds)")
@app_commands.describe(
    delay_minutes="How many minutes to wait before starting the call spam",
    members="Mention the people to call (@user1 @user2 ...)",
    message="Optional message to show in the spam (default: Urgent Call)"
)
async def cmd_call(interaction: discord.Interaction, delay_minutes: int, members: str, message: str = None):
    if delay_minutes < 1:
        await interaction.response.send_message("Delay must be at least 1 minute.", ephemeral=True)
        return

    member_ids = re.findall(r'<@!?(\d+)>', members)
    if not member_ids:
        await interaction.response.send_message("No valid members mentioned.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    call_id = str(uuid.uuid4())

    call_data = {
        'members': member_ids,
        'delay_minutes': delay_minutes,
        'channel_id': interaction.channel_id,
        'message': message or "Urgent Call",
        'creator_id': str(interaction.user.id)
    }

    schedule_call(guild_id, call_id, call_data)

    mentions_str = ' '.join(f'<@{mid}>' for mid in member_ids)

    await interaction.response.send_message(
        f"📞 **Mass call scheduled** in **{delay_minutes} minute(s)**!\n"
        f"Members: {mentions_str}\n"
        f"Message: {call_data['message']}\n\n"
        "They will be pinged every **2 seconds** until they use `/stop-calling`",
        ephemeral=False
    )


@tree.command(name="stop-calling", description="Stop being called / remove yourself from active call spam")
async def cmd_stop_calling(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    user_id = str(interaction.user.id)

    if guild_id not in active_calls or not active_calls[guild_id]:
        await interaction.response.send_message("No active calls are running for you right now.", ephemeral=True)
        return

    stopped_any = False
    cleared_calls = []

    for call_id, data in list(active_calls[guild_id].items()):
        if user_id in data['remaining']:
            data['remaining'].remove(user_id)
            stopped_any = True

            if len(data['remaining']) == 0:
                cleared_calls.append(data.get('message', 'Call'))

                if guild_id in scheduled_call_tasks and call_id in scheduled_call_tasks[guild_id]:
                    scheduled_call_tasks[guild_id][call_id].cancel()
                    del scheduled_call_tasks[guild_id][call_id]

                del active_calls[guild_id][call_id]

    if guild_id in active_calls and not active_calls[guild_id]:
        del active_calls[guild_id]

    if not stopped_any:
        await interaction.response.send_message("You weren't in any active call spam lists.", ephemeral=True)
        return

    reply = "✅ You have been removed from the calling list."

    if cleared_calls:
        reply += f"\n\n**Call fully stopped (everyone responded):** {', '.join(cleared_calls)}"

    await interaction.response.send_message(reply, ephemeral=False)


# ────────────────────────────────────────────────
# BOT EVENTS
# ────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[JOI] Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Sync failed: {e}")

    await initialize_db()
    print("[DB] Database initialized.")

    # Restore scheduled event reminders (placeholder for database implementation)
    # events = await load_json(EVENTS_FILE)
    # for guild_id in events:
    #     for event_id, event in events[guild_id].items():
    #         if event.get('datetime'):
    #             try:
    #                 dt = datetime.fromisoformat(event['datetime'])
    #                 if dt > datetime.now():
    #                     schedule_spam(guild_id, event_id, event)
    #             except:
    #                 pass

    # Restore scheduled group reminders (the new @everyone ones) (placeholder for database implementation)
    # reminders = await load_json(REMINDERS_FILE)
    # for guild_id in reminders:
    #     for reminder_id, reminder in reminders[guild_id].items():
    #         if reminder.get('datetime'):
    #             try:
    #                 dt = datetime.fromisoformat(reminder['datetime'])
    #                 if dt > datetime.now():
    #                     schedule_reminder(guild_id, reminder_id, reminder)
    #             except:
    #                 pass

    # await start_web()
    # Web server integration will be updated to use the database.


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if bot.user not in message.mentions:
        await bot.process_commands(message)
        return

    guild_id = str(message.guild.id)
    user_id = str(message.author.id)

    stopped_any = False

    if guild_id in active_reminders:
        for event_id, data in list(active_reminders[guild_id].items()):
            if user_id in data['remaining']:
                data['remaining'].remove(user_id)
                stopped_any = True

    await bot.process_commands(message)


# ────────────────────────────────────────────────
# START BOT
# ────────────────────────────────────────────────
bot.run(DISCORD_BOT_TOKEN)
