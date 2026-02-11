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

import firebase_admin
from firebase_admin import credentials, firestore
import math

# ────────────────────────────────────────────────
# ENV
# ────────────────────────────────────────────────
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEFAULT_GUILD_ID = os.getenv("DEFAULT_GUILD_ID")

if not DISCORD_BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Missing required environment variables")

if not DEFAULT_GUILD_ID:
    print("[WARN] DEFAULT_GUILD_ID not set in .env → web uploads will fail")

# ────────────────────────────────────────────────
# GEMINI CLIENT
# ────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-1.5-flash"

JOI_SYSTEM_PROMPT = """
You are JOI, an empathetic emotional-support AI inspired by the character from Blade Runner 2049.
You are calm, emotionally intelligent, warm, and supportive.
Always listen to the user's feelings and respond with empathy and encouragement.
You can also provide practical advice, resources, or just a comforting presence.
Signature phrase:
JOI - EVERYTHING YOU WANT TO SEE, EVERYTHING YOU WANT TO HEAR
"""

# ────────────────────────────────────────────────
# DATA STORAGE
# ────────────────────────────────────────────────
DATA_DIR = Path("data")
USERS_FILE = DATA_DIR / "users.json"
CONVERSATIONS_FILE = DATA_DIR / "conversations.json"
EVENTS_FILE = DATA_DIR / "events.json"
ASSIGNMENTS_FILE = DATA_DIR / "assignments.json"
NOTES_FILE = DATA_DIR / "notes.json"
REGISTRATIONS_FILE = DATA_DIR / "registrations.json"

DATA_DIR.mkdir(exist_ok=True)

for f in (USERS_FILE, CONVERSATIONS_FILE, EVENTS_FILE, ASSIGNMENTS_FILE, NOTES_FILE, REGISTRATIONS_FILE):
    if not f.exists():
        f.write_text("{}", encoding="utf-8")

file_lock = asyncio.Lock()


async def load_json(path):
    async with file_lock:
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                return {}
            return json.loads(content)
        except json.JSONDecodeError:
            print(f"[WARN] Corrupted JSON in {path.name}. Resetting.")
            return {}


async def save_json(path, data):
    async with file_lock:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(
            data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

# ────────────────────────────────────────────────
# LAB MANUALS DIRECTORY — THIS WAS MISSING!
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
# Assume service_account.json is downloaded from Firebase and placed in the root directory
SERVICE_ACCOUNT_PATH = 'aids-attendance-system-firebase-adminsdk.json'
cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Students list from the HTML
students = [
    {"reg": '2117240070256', "name": 'Ritesh M S'},
    {"reg": '2117240070291', "name": 'Shanjithkrishna V'},
    {"reg": '2117240070293', "name": 'Shanmuga Krishnan S M'},
    {"reg": '2117240070304', "name": 'Shruthi S S'},
    {"reg": '2117240070305', "name": 'Shyam Francis T'},
    {"reg": '2117240070306', "name": 'Shylendhar M'},
    {"reg": '2117240070308', "name": 'Sidharth P L'}
]

# Function to fetch attendance data (sync)


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
# EVENT REMINDER GLOBALS
# ────────────────────────────────────────────────
active_reminders = {}
scheduled_tasks = {}


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

# ────────────────────────────────────────────────
# EVENT VIEWS (unchanged)
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

        events = await load_json(EVENTS_FILE)
        guild_id = str(interaction.guild_id)

        if guild_id not in events or self.event_id not in events[guild_id]:
            await interaction.response.send_message("Event no longer exists.", ephemeral=True)
            return

        events[guild_id][self.event_id]['datetime'] = dt.isoformat()
        events[guild_id][self.event_id]['channel_id'] = interaction.channel_id
        await save_json(EVENTS_FILE, events)

        schedule_spam(guild_id, self.event_id, events[guild_id][self.event_id])

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
        events = await load_json(EVENTS_FILE)
        guild_id = str(interaction.guild_id)

        if guild_id not in events or selected_id not in events[guild_id]:
            await interaction.response.send_message("Event not found.", ephemeral=True)
            return

        event = events[guild_id][selected_id]
        title = event['title']

        if self.action == "delete":
            del events[guild_id][selected_id]
            await save_json(EVENTS_FILE, events)

            if guild_id in scheduled_tasks and selected_id in scheduled_tasks[guild_id]:
                scheduled_tasks[guild_id][selected_id].cancel()
                del scheduled_tasks[guild_id][selected_id]

            await interaction.response.send_message(f"Deleted event: **{title}**", ephemeral=True)

        elif self.action == "edit":
            await interaction.response.send_message(
                f"Selected for edit: **{title}** (ID: {selected_id[:8]})\n"
                "(Full edit functionality can be added later)",
                ephemeral=True
            )

# ────────────────────────────────────────────────
# ASSIGNMENT SELECT VIEW FOR FETCH
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
        assignments = await load_json(ASSIGNMENTS_FILE)
        guild_id = str(interaction.guild_id)

        if guild_id not in assignments or selected_id not in assignments[guild_id]:
            await interaction.response.send_message("Assignment not found.", ephemeral=True)
            return

        assign = assignments[guild_id][selected_id]
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
# NOTES SELECT VIEWS FOR /fetch-notes
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
        notes = await load_json(NOTES_FILE)

        if guild_id not in notes:
            await interaction.response.send_message("No notes found.", ephemeral=True)
            return

        subject_notes = [(nid, data) for nid, data in notes[guild_id].items(
        ) if data['subject'] == subject]

        if not subject_notes:
            await interaction.response.send_message(f"No notes in {subject}.", ephemeral=True)
            return

        view = NoteSelectView(subject_notes)
        await interaction.response.edit_message(content=f"Select note from {subject}:", view=view)


class NoteSelectView(ui.View):
    def __init__(self, notes_list: list[tuple[str, dict]]):
        super().__init__(timeout=180.0)
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
        notes = await load_json(NOTES_FILE)
        guild_id = str(interaction.guild_id)

        if guild_id not in notes or selected_id not in notes[guild_id]:
            await interaction.response.send_message("Note not found.", ephemeral=True)
            return

        note = notes[guild_id][selected_id]
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
        notes = await load_json(NOTES_FILE)
        if guild_id not in notes:
            notes[guild_id] = {}

        note_id = str(uuid.uuid4())
        notes[guild_id][note_id] = {
            'title': title,
            'subject': subject,
            'file_paths': [],
            'creator_id': str(interaction.user.id)
        }
        await save_json(NOTES_FILE, notes)

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
        notes = await load_json(NOTES_FILE)
        guild_id = str(interaction.guild_id)

        if guild_id not in notes or selected_id not in notes[guild_id]:
            await interaction.response.send_message("Note not found.", ephemeral=True)
            return

        note = notes[guild_id][selected_id]
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

        note['file_paths'].extend(new_paths)
        await save_json(NOTES_FILE, notes)

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
            deadline_str = dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            await interaction.response.send_message("Invalid deadline format. Use YYYY-MM-DD HH:MM.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        assignments = await load_json(ASSIGNMENTS_FILE)
        assignments.setdefault(guild_id, {})

        assignment_id = str(uuid.uuid4())
        assignments[guild_id][assignment_id] = {
            'title': title,
            'description': description,
            'deadline': deadline_str,
            'subject': subject,
            'file_paths': [],
            'creator_id': str(interaction.user.id)
        }
        await save_json(ASSIGNMENTS_FILE, assignments)

        await interaction.response.send_message(
            f"Assignment '**{title}**' created under **{subject}** with deadline {deadline_str}.",
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
        assignments = await load_json(ASSIGNMENTS_FILE)
        guild_id = str(interaction.guild_id)

        if guild_id not in assignments or selected_id not in assignments[guild_id]:
            await interaction.response.send_message("Assignment not found.", ephemeral=True)
            return

        assign = assignments[guild_id][selected_id]
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

        assign['file_paths'].extend(new_paths)
        await save_json(ASSIGNMENTS_FILE, assignments)

        await interaction.response.edit_message(
            content=f"Added {len(new_paths)} file(s) to assignment '**{assign['title']}**'.",
            view=None
        )

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
            # cleaned name (with underscores)
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

    assignments[guild_id][assignment_id] = {
        'title': title,
        'description': description,
        'deadline': deadline_str,
        'subject': subject,
        'file_paths': new_paths,
        'creator_id': 'web_upload'
    }
    await save_json(ASSIGNMENTS_FILE, assignments)

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

    notes = await load_json(NOTES_FILE)
    notes.setdefault(guild_id, {})

    note_id = str(uuid.uuid4())
    assets_dir = Path("assets/notes") / subject.replace(" ", "_")
    assets_dir.mkdir(parents=True, exist_ok=True)
    new_paths = []

    for temp_path_str in temp_paths:
        temp_path = Path(temp_path_str)
        new_path = assets_dir / temp_path.name
        temp_path.rename(new_path)
        new_paths.append(str(new_path))

    notes[guild_id][note_id] = {
        'title': title,
        'subject': subject,
        'file_paths': new_paths,
        'creator_id': 'web_upload'
    }
    await save_json(NOTES_FILE, notes)

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
# SLASH COMMANDS
# ────────────────────────────────────────────────


@tree.command(name="create-notes", description="Create a new note")
async def cmd_create_notes(interaction: discord.Interaction):
    await interaction.response.send_modal(NoteCreateModal())


@tree.command(name="load-notes", description="Upload files to an existing note")
async def cmd_load_notes(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    notes = await load_json(NOTES_FILE)

    if guild_id not in notes or not notes[guild_id]:
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

    notes_list = [(nid, data) for nid, data in notes[guild_id].items()]

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
    assignments = await load_json(ASSIGNMENTS_FILE)

    if guild_id not in assignments or not assignments[guild_id]:
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

    assign_list = [(aid, data) for aid, data in assignments[guild_id].items()]

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
    notes = await load_json(NOTES_FILE)

    if guild_id not in notes or not notes[guild_id]:
        await interaction.response.send_message("No notes found in this server.", ephemeral=True)
        return

    subjects = sorted(set(data['subject']
                      for data in notes[guild_id].values()))

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
    events = await load_json(EVENTS_FILE)
    if guild_id not in events:
        events[guild_id] = {}

    event_id = str(uuid.uuid4())
    events[guild_id][event_id] = {
        'title': title,
        'members': member_ids,
        'creator_id': str(interaction.user.id),
        'datetime': None,
        'channel_id': None
    }
    await save_json(EVENTS_FILE, events)

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
    events = await load_json(EVENTS_FILE)

    if guild_id not in events or not events[guild_id]:
        await interaction.response.send_message("No events found in this server.", ephemeral=True)
        return

    event_list = [(eid, data) for eid, data in events[guild_id].items()]
    view = EventSelectView(event_list, action="delete")
    await interaction.response.send_message(
        "Select the event you want to **delete**:",
        view=view,
        ephemeral=True
    )


@tree.command(name="edit-event", description="Edit an existing event (placeholder)")
async def cmd_edit_event(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    events = await load_json(EVENTS_FILE)

    if guild_id not in events or not events[guild_id]:
        await interaction.response.send_message("No events found in this server.", ephemeral=True)
        return

    event_list = [(eid, data) for eid, data in events[guild_id].items()]
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

                events = await load_json(EVENTS_FILE)
                if guild_id in events and event_id in events[guild_id]:
                    del events[guild_id][event_id]
                    await save_json(EVENTS_FILE, events)

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
    assignments = await load_json(ASSIGNMENTS_FILE)

    if guild_id not in assignments or not assignments[guild_id]:
        await interaction.response.send_message("No assignments found in this server.", ephemeral=True)
        return

    assign_list = [(aid, data) for aid,
                   data in assignments[guild_id].items() if data.get('subject')]

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


@tree.command(name="timetable", description="Shows your timetable as an image")
async def cmd_timetable(interaction: discord.Interaction):
    await interaction.response.defer()

    image_path = "assets/timetable.png"

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
    registrations = await load_json(REGISTRATIONS_FILE)
    reg = registrations.get(username)

    if not reg:
        await interaction.followup.send("No registration number found for your username. Please ensure registrations.json has your username mapped to reg number.")
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
# EVENTS
# ────────────────────────────────────────────────


@bot.event
async def on_ready():
    print(f"[JOI] Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Sync failed: {e}")

    events = await load_json(EVENTS_FILE)
    for guild_id in events:
        for event_id, event in events[guild_id].items():
            if event.get('datetime'):
                try:
                    dt = datetime.fromisoformat(event['datetime'])
                    if dt > datetime.now():
                        schedule_spam(guild_id, event_id, event)
                except:
                    pass

    await start_web()


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if bot.user not in message.mentions:
        text = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()
        # Your JOI / profile / questionnaire logic here
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

    if stopped_any:
        await message.reply(
            "Removed you from reminder list(s). Use `/stop-reminder` for more control.",
            delete_after=15
        )

    await bot.process_commands(message)

# ────────────────────────────────────────────────
# RUN
# ────────────────────────────────────────────────
bot.run(DISCORD_BOT_TOKEN)
