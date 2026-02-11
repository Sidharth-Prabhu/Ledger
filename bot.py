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
CALLS_FILE = DATA_DIR / "calls.json"  # New file for call spam sessions

DATA_DIR.mkdir(exist_ok=True)

for f in (USERS_FILE, CONVERSATIONS_FILE, EVENTS_FILE, ASSIGNMENTS_FILE, NOTES_FILE, CALLS_FILE):
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
# BOT SETUP
# ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ────────────────────────────────────────────────
# EVENT REMINDER GLOBALS
# ────────────────────────────────────────────────
active_reminders = {}
scheduled_tasks = {}

# ────────────────────────────────────────────────
# CALL SPAM GLOBALS (parallel structure to reminders)
# ────────────────────────────────────────────────
# guild_id → {call_id → {'remaining': [...], 'channel': ..., 'end_time': ...}}
active_calls = {}
scheduled_call_tasks = {}       # guild_id → {call_id → Task}


def schedule_call_spam(guild_id: str, call_id: str, call_data: dict):
    async def inner():
        try:
            # Optional delay before starting spam
            delay_minutes = call_data.get('start_after', 0)
            if delay_minutes > 0:
                await asyncio.sleep(delay_minutes * 60)

            channel = bot.get_channel(call_data['channel_id'])
            if not channel:
                return

            active_calls.setdefault(guild_id, {})[call_id] = {
                'remaining': call_data['members'][:],
                'channel': channel
            }

            # Infinite loop until manually stopped or no one left
            while active_calls.get(guild_id, {}).get(call_id):
                remaining = active_calls[guild_id][call_id]['remaining']
                if not remaining:
                    break
                mentions = ' '.join(f"<@{uid}>" for uid in remaining)
                await channel.send(f"**CALLING!** Join the voice channel! {mentions}")
                await asyncio.sleep(2)  # 2 seconds interval

            # Cleanup when done
            active_calls[guild_id].pop(call_id, None)
            if not active_calls[guild_id]:
                active_calls.pop(guild_id, None)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Call spam error {call_id}: {e}")
        finally:
            scheduled_call_tasks.get(guild_id, {}).pop(call_id, None)

    task = asyncio.create_task(inner())
    scheduled_call_tasks.setdefault(guild_id, {})[call_id] = task
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
# MODAL & VIEW FOR NOTES
# ────────────────────────────────────────────────


class NoteCreateModal(ui.Modal, title="Create New Note"):
    note_title = ui.TextInput(
        label="Title",
        placeholder="e.g. Math Notes Chapter 1",
        required=True
    )
    note_subject = ui.TextInput(
        label="Subject",
        placeholder="e.g. Math",
        required=True
    )

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

# ────────────────────────────────────────────────
# NEW MODAL & VIEW FOR ASSIGNMENTS (similar to notes)
# ────────────────────────────────────────────────


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
# NEW CALL SPAM COMMANDS
# ────────────────────────────────────────────────


@tree.command(name="call", description="Start calling mentioned users every 2 seconds (after optional delay)")
@app_commands.describe(
    delay_minutes="Delay before starting the spam (in minutes, default 0)",
    members="Mention members with @ (space separated)"
)
async def cmd_call(interaction: discord.Interaction, delay_minutes: int = 0, members: str = ""):
    member_ids = re.findall(r'<@!?(\d+)>', members)
    if not member_ids:
        await interaction.response.send_message("No valid members mentioned.", ephemeral=True)
        return

    if delay_minutes < 0:
        await interaction.response.send_message("Delay cannot be negative.", ephemeral=True)
        return

    guild_id = str(interaction.guild_id)
    calls = await load_json(CALLS_FILE)
    calls.setdefault(guild_id, {})

    call_id = str(uuid.uuid4())
    calls[guild_id][call_id] = {
        'members': member_ids,
        'creator_id': str(interaction.user.id),
        'channel_id': interaction.channel_id,
        'start_after': delay_minutes  # minutes to wait before first ping
    }
    await save_json(CALLS_FILE, calls)

    mentions_str = ' '.join(f'<@{mid}>' for mid in member_ids)

    # Schedule the spam (with delay if any)
    schedule_call_spam(guild_id, call_id, calls[guild_id][call_id])

    delay_text = f"after {delay_minutes} minutes" if delay_minutes > 0 else "immediately"
    await interaction.response.send_message(
        f"Started calling {mentions_str} every **2 seconds** ({delay_text}).\n"
        "Use `/stop-calling` to stop being pinged.",
        ephemeral=False
    )


@tree.command(name="stop-calling", description="Stop being pinged by active call spam")
async def cmd_stop_calling(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    user_id = str(interaction.user.id)

    if guild_id not in active_calls or not active_calls[guild_id]:
        await interaction.response.send_message("You are not currently being called.", ephemeral=True)
        return

    stopped = False

    for call_id, data in list(active_calls[guild_id].items()):
        if user_id in data['remaining']:
            data['remaining'].remove(user_id)
            stopped = True

            # If no one left, fully stop this call
            if not data['remaining']:
                if guild_id in scheduled_call_tasks and call_id in scheduled_call_tasks[guild_id]:
                    scheduled_call_tasks[guild_id][call_id].cancel()
                    del scheduled_call_tasks[guild_id][call_id]
                del active_calls[guild_id][call_id]

                calls = await load_json(CALLS_FILE)
                if guild_id in calls and call_id in calls[guild_id]:
                    del calls[guild_id][call_id]
                    await save_json(CALLS_FILE, calls)

    if guild_id in active_calls and not active_calls[guild_id]:
        del active_calls[guild_id]

    if stopped:
        await interaction.response.send_message("You have been removed from active call spam.", ephemeral=False)
    else:
        await interaction.response.send_message("You weren't in any active call lists.", ephemeral=True)

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
