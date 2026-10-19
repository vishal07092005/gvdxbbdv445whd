import os
import re
import sys
import time
import math
import json
import shutil
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, unquote
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

# ================= SECRETS & ADMIN CONFIG =================
API_ID_STR = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID_STR = os.environ.get("ADMIN_ID", "").strip()

if not API_ID_STR or not API_HASH or not BOT_TOKEN:
    print("❌ ERROR: GitHub Secrets (API_ID, API_HASH, BOT_TOKEN) खाली हैं!")
    sys.exit(1)

API_ID = int(API_ID_STR)

# Multiple Admins support (comma ya space se separate kar sakte ho)
ADMINS = set()
for x in re.split(r"[,\s]+", ADMIN_ID_STR):
    if x.isdigit():
        ADMINS.add(int(x))

DAILY_LIMIT = 8  # Normal users ke liye 8 videos per day
DB_FILE = "users_db.json"
IST = timezone(timedelta(hours=5, minutes=30))  # Indian Standard Time

bot = Client(
    "pro_uploader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    workers=16,
    max_concurrent_transmissions=8
)

ACTIVE_PROCESSES = {}
CANCELLED_TASKS = set()

# ================= DATABASE ENGINE (JSON) =================
def get_today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}, "total_downloads": 0}

def save_db(data: dict):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"DB Save Error: {e}")

DB = load_db()

def register_and_get_user(user) -> dict:
    uid = str(user.id)
    today = get_today_ist()
    now_str = datetime.now(IST).strftime("%Y-%m-%d %I:%M %p")
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Unknown"
    username = f"@{user.username}" if user.username else "No Username"

    if uid not in DB["users"]:
        DB["users"][uid] = {
            "name": full_name,
            "username": username,
            "joined_at": now_str,
            "last_active": now_str,
            "last_date": today,
            "today_count": 0,
            "total_count": 0
        }
    else:
        u_data = DB["users"][uid]
        u_data["name"] = full_name
        u_data["username"] = username
        u_data["last_active"] = now_str
        # Agar naya din shuru ho gaya hai toh daily count 0 kar do
        if u_data.get("last_date") != today:
            u_data["last_date"] = today
            u_data["today_count"] = 0

    save_db(DB)
    return DB["users"][uid]

def increment_user_download(user):
    u_data = register_and_get_user(user)
    u_data["today_count"] += 1
    u_data["total_count"] += 1
    DB["total_downloads"] = DB.get("total_downloads", 0) + 1
    save_db(DB)
    return u_data

# ================= HELPER FUNCTIONS =================
def humanbytes(size):
    if not size:
        return "0 B"
    power = 1024
    n = 0
    dic_powerN = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB'}
    while size >= power and n < 3:
        size /= power
        n += 1
    return f"{round(size, 2)} {dic_powerN[n]}"

def format_time(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"

def get_progress_bar(percentage):
    filled = max(0, min(10, math.floor(percentage / 10)))
    return "▰" * filled + "▱" * (10 - filled)

def extract_real_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    qs = parse_qs(parsed.query)
    if "url" in qs:
        return unquote(qs["url"][0])
    return raw_url

async def get_video_metadata(file_path: str):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        file_path
    ]
    width, height, duration = 1280, 720, 0
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await dur_out_comm(proc)
        data = json.loads(stdout.decode("utf-8", errors="ignore"))

        if "format" in data and "duration" in data["format"]:
            duration = int(float(data["format"]["duration"]))

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = int(stream.get("width", 1280))
                height = int(stream.get("height", 720))
                if duration == 0 and "duration" in stream:
                    duration = int(float(stream["duration"]))
                break
    except Exception:
        pass
    return width, height, duration

async def dur_out_comm(proc):
    return await proc.communicate()

# ================= UPLOAD PROGRESS UI =================
async def upload_progress(current, total, status_msg: Message, start_time, title, task_id):
    if task_id in CANCELLED_TASKS:
        raise asyncio.CancelledError("Upload cancelled by user")

    now = time.time()
    diff = now - start_time
    if round(diff % 3.00) == 0 or current == total:
        percentage = current * 100 / total if total > 0 else 0
        speed = current / diff if diff > 0 else 0
        eta = round((total - current) / speed) if speed > 0 else 0
        bar = get_progress_bar(percentage)

        text = (
            f"⚡ **PRO LECTURE ENGINE — UPLOADING**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎬 **Title:** `{title}`\n\n"
            f"📊 **Progress:** [{bar}] `{percentage:.1f}%`\n"
            f"📦 **Uploaded:** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
            f"🚀 **Speed:** `{humanbytes(speed)}/s`\n"
            f"⏳ **ETA:** `{format_time(eta)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        try:
            await status_msg.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛑 Cancel Task", callback_data=f"cancel_{task_id}")]
                ])
            )
        except Exception:
            pass

# ================= USER COMMANDS =================
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    u_data = register_and_get_user(message.from_user)
    is_admin = message.from_user.id in ADMINS
    role_badge = "👑 **Admin (Unlimited Access)**" if is_admin else f"👤 **User ({u_data['today_count']}/{DAILY_LIMIT} Today)**"

    admin_help = (
        "\n\n🛠️ **Admin Panel Commands:**\n"
        "• `/stats` — Live Users & Download Analytics\n"
        "• `/users` — Download Full Users Report (.txt)\n"
        "• `/user <id>` — Check Specific User History\n"
        "• `/reset <id>` — Reset User's Daily Limit\n"
        "• `/broadcast <msg>` — Send Message to All Users"
    ) if is_admin else ""

    await message.reply_text(
        f"⚡ **SPIDY PRO LECTURE DOWNLOADER v3.0** ⚡\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎖️ **Status:** {role_badge}\n"
        f"🚀 **Engine:** `32x Multi-Thread Turbo`\n"
        f"📊 **Your Total Downloads:** `{u_data['total_count']} Videos`\n\n"
        f"📝 **Kaise Use Karein:**\n"
        f"`https://link.m3u8 | Lecture 01 - Physics`\n"
        f"• Apna quota check karne ke liye `/myplan` bhejein."
        f"{admin_help}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

@bot.on_message(filters.command("myplan") & filters.private)
async def myplan_handler(client, message: Message):
    u_data = register_and_get_user(message.from_user)
    is_admin = message.from_user.id in ADMINS

    if is_admin:
        limit_str = "♾️ Unlimited (Admin)"
        rem_str = "♾️ Unlimited"
    else:
        used = u_data["today_count"]
        rem = max(0, DAILY_LIMIT - used)
        limit_str = f"{used} / {DAILY_LIMIT} Videos"
        rem_str = f"{rem} Videos Left Today"

    await message.reply_text(
        f"📊 **YOUR ACCOUNT STATISTICS**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Name:** `{u_data['name']}`\n"
        f"🆔 **User ID:** `{message.from_user.id}`\n"
        f"📅 **Today's Usage:** `{limit_str}`\n"
        f"🔋 **Remaining Quota:** `{rem_str}`\n"
        f"🏆 **Lifetime Downloads:** `{u_data['total_count']} Videos`\n"
        f"🕒 **Reset Time:** `12:00 AM IST (Midnight)`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

# ================= ADMIN COMMANDS =================
@bot.on_message(filters.command("stats") & filters.private)
async def stats_handler(client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply_text("⛔ **Access Denied!** Ye command sirf Bot Admin ke liye hai.")
        return

    today = get_today_ist()
    users = DB.get("users", {})
    total_users = len(users)
    total_dl = DB.get("total_downloads", 0)

    active_today = 0
    today_dl = 0
    for uid, u in users.items():
        if u.get("last_date") == today:
            if u.get("today_count", 0) > 0:
                active_today += 1
            today_dl += u.get("today_count", 0)

    # Sort top 15 users by total downloads
    sorted_users = sorted(
        users.items(),
        key=lambda item: item[1].get("total_count", 0),
        reverse=True
    )[:15]

    leaderboard = ""
    for idx, (uid, u) in enumerate(sorted_users, 1):
        t_cnt = u.get("today_count", 0) if u.get("last_date") == today else 0
        leaderboard += (
            f"**{idx}.** `{u.get('name')}` ({u.get('username')})\n"
            f"   ├ 🆔 `{uid}`\n"
            f"   └ 📅 Today: `{t_cnt}/{DAILY_LIMIT}` | 🏆 Total: `{u.get('total_count', 0)}`\n"
        )

    if not leaderboard:
        leaderboard = "`Abhi tak kisi user ne use nahi kiya hai.`"

    text = (
        f"👑 **ADMIN ANALYTICS DASHBOARD**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 **Total Registered Users:** `{total_users}`\n"
        f"🔥 **Active Users Today:** `{active_today}`\n"
        f"📥 **Downloads Today:** `{today_dl} Videos`\n"
        f"🌎 **Lifetime Downloads:** `{total_dl} Videos`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 **TOP 15 USERS (USAGE REPORT):**\n\n"
        f"{leaderboard}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *Poori list file me lene ke liye `/users` bhejein.*"
    )
    await message.reply_text(text)

@bot.on_message(filters.command("users") & filters.private)
async def export_users_handler(client, message: Message):
    if message.from_user.id not in ADMINS:
        return

    today = get_today_ist()
    users = DB.get("users", {})
    report_path = "/tmp/all_users_report.txt"

    sorted_users = sorted(
        users.items(),
        key=lambda item: item[1].get("total_count", 0),
        reverse=True
    )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("====================================================\n")
        f.write(f"         COMPLETE BOT USERS REPORT ({today})        \n")
        f.write(f"  Total Users: {len(users)} | Total Downloads: {DB.get('total_downloads', 0)}\n")
        f.write("====================================================\n\n")
        for idx, (uid, u) in enumerate(sorted_users, 1):
            t_cnt = u.get("today_count", 0) if u.get("last_date") == today else 0
            f.write(
                f"{idx}. Name: {u.get('name')} ({u.get('username')})\n"
                f"   User ID      : {uid}\n"
                f"   Today Used   : {t_cnt} / {DAILY_LIMIT}\n"
                f"   Total Videos : {u.get('total_count', 0)}\n"
                f"   Joined On    : {u.get('joined_at', 'N/A')}\n"
                f"   Last Active  : {u.get('last_active', 'N/A')}\n"
                f"----------------------------------------------------\n"
            )

    await message.reply_document(
        document=report_path,
        caption=f"📋 **Full Users Analytics Report**\n👥 **Total Users:** `{len(users)}`"
    )
    if os.path.exists(report_path):
        os.remove(report_path)

@bot.on_message(filters.command("user") & filters.private)
async def single_user_info(client, message: Message):
    if message.from_user.id not in ADMINS:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply_text("⚠️ **Usage:** `/user <user_id>`")
        return

    uid = parts[1].strip()
    u = DB.get("users", {}).get(uid)
    if not u:
        await message.reply_text(f"❌ User ID `{uid}` database me nahi mila!")
        return

    today = get_today_ist()
    t_cnt = u.get("today_count", 0) if u.get("last_date") == today else 0
    await message.reply_text(
        f"🔍 **USER PROFILE DETAILS**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Name:** `{u.get('name')}`\n"
        f"🔗 **Username:** {u.get('username')}\n"
        f"🆔 **User ID:** `{uid}`\n"
        f"📅 **Today's Downloads:** `{t_cnt} / {DAILY_LIMIT}`\n"
        f"🏆 **Total Downloads:** `{u.get('total_count', 0)} Videos`\n"
        f"📆 **Joined Date:** `{u.get('joined_at')}`\n"
        f"⏱️ **Last Active:** `{u.get('last_active')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

@bot.on_message(filters.command("reset") & filters.private)
async def reset_user_limit(client, message: Message):
    if message.from_user.id not in ADMINS:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply_text("⚠️ **Usage:** `/reset <user_id>`")
        return

    uid = parts[1].strip()
    if uid in DB.get("users", {}):
        DB["users"][uid]["today_count"] = 0
        DB["users"][uid]["last_date"] = get_today_ist()
        save_db(DB)
        await message.reply_text(f"✅ User `{uid}` ({DB['users'][uid]['name']}) ki aaj ki limit `0/{DAILY_LIMIT}` reset kar di gayi hai!")
    else:
        await message.reply_text(f"❌ User ID `{uid}` database me nahi mila.")

@bot.on_message(filters.command("broadcast") & filters.private)
async def broadcast_handler(client, message: Message):
    if message.from_user.id not in ADMINS:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("⚠️ **Usage:** `/broadcast <Aapka Message>`")
        return

    b_msg = parts[1]
    users = list(DB.get("users", {}).keys())
    sent, failed = 0, 0
    sts = await message.reply_text(f"📢 **Broadcasting message to {len(users)} users...**")

    for uid in users:
        try:
            await client.send_message(int(uid), f"📢 **ADMIN ANNOUNCEMENT**\n━━━━━━━━━━━━━━━━━━━━━━\n\n{b_msg}")
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1

    await sts.edit_text(f"✅ **Broadcast Complete!**\n\n🟢 **Sent:** `{sent}`\n🔴 **Failed/Blocked:** `{failed}`")

# ================= CANCEL HANDLER =================
@bot.on_callback_query(filters.regex(r"^cancel_(\d+)$"))
async def cancel_handler(client, callback_query: CallbackQuery):
    task_id = int(callback_query.matches[0].group(1))
    CANCELLED_TASKS.add(task_id)

    proc = ACTIVE_PROCESSES.get(task_id)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass

    await callback_query.answer("🛑 Task Cancelled!", show_alert=True)
    try:
        await callback_query.message.edit_text("🛑 **Task Cancelled by User!** *(Aapki daily limit cut nahi hui hai)*")
    except Exception:
        pass

# ================= MAIN DOWNLOAD HANDLER =================
@bot.on_message(filters.private & filters.text & ~filters.command(["start", "myplan", "stats", "users", "user", "reset", "broadcast"]))
async def handle_link(client, message: Message):
    user = message.from_user
    u_data = register_and_get_user(user)
    is_admin = user.id in ADMINS

    # 🛑 CHECK DAILY LIMIT (8 VIDEOS FOR NORMAL USERS, UNLIMITED FOR ADMIN)
    if not is_admin and u_data["today_count"] >= DAILY_LIMIT:
        await message.reply_text(
            f"🛑 **DAILY LIMIT REACHED ({DAILY_LIMIT}/{DAILY_LIMIT})!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Bhai aap aaj ki **{DAILY_LIMIT} Videos** download kar chuke hain.\n"
            f"Server load control me rakhne ke liye har user ko roz **{DAILY_LIMIT} videos** milte hain.\n\n"
            f"⏳ **Next Reset:** `Aaj Raat 12:00 AM (IST)`\n"
            f"🏆 **Your Total Downloads:** `{u_data['total_count']} Videos`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        return

    text = message.text.strip()
    if not text.startswith("http"):
        await message.reply_text("❌ **Invalid Format!** Link `http` ya `https` se shuru hona chahiye.")
        return

    if "|" in text:
        raw_url, title = [x.strip() for x in text.split("|", 1)]
    else:
        parts = text.split(maxsplit=1)
        raw_url = parts[0]
        title = parts[1] if len(parts) > 1 else f"Lecture_{message.id}"

    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or f"Lecture_{message.id}"
    m3u8_url = extract_real_url(raw_url)
    task_id = message.id

    work_dir = f"/tmp/dl_{task_id}"
    os.makedirs(work_dir, exist_ok=True)
    output_mp4 = os.path.join(work_dir, f"{safe_title}.mp4")
    thumb_jpg = os.path.join(work_dir, "thumb.jpg")

    cancel_Btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel Task", callback_data=f"cancel_{task_id}")]
    ])

    quota_tag = "♾️ Admin (Unlimited)" if is_admin else f"{u_data['today_count'] + 1}/{DAILY_LIMIT} Today"

    status_msg = await message.reply_text(
        f"⚡ **INITIALIZING 32X TURBO ENGINE...**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 **Title:** `{safe_title}`\n"
        f"📊 **Quota:** `{quota_tag}`\n"
        f"🔗 **Status:** `Connecting to Akamai CDN...`",
        reply_markup=cancel_Btn
    )

    task_start_time = time.time()

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--newline",
            "--no-check-certificate",
            "--concurrent-fragments", "32",
            "--http-chunk-size", "10M",
            "--buffer-size", "64K",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "--referer", "https://appx-play.akamai.net.in/",
            "--add-header", "Origin:https://appx-play.akamai.net.in",
            "--remux-video", "mp4",
            "-o", output_mp4,
            m3u8_url
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        ACTIVE_PROCESSES[task_id] = process

        last_update = 0
        error_logs = []
        pct_regex = re.compile(r"(\d+\.\d+)%")
        speed_regex = re.compile(r"at\s+([0-9.]+[KMG]i?B/s)")
        eta_regex = re.compile(r"ETA\s+([0-9:]+)")

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            if task_id in CANCELLED_TASKS:
                break

            line_str = line.decode("utf-8", errors="ignore").strip()
            if line_str:
                error_logs.append(line_str)
                if len(error_logs) > 8:
                    error_logs.pop(0)

            if "[download]" in line_str and "%" in line_str:
                now = time.time()
                if now - last_update >= 3:
                    last_update = now
                    pct_match = pct_regex.search(line_str)
                    spd_match = speed_regex.search(line_str)
                    eta_match = eta_regex.search(line_str)

                    pct_val = float(pct_match.group(1)) if pct_match else 0.0
                    spd_val = spd_match.group(1) if spd_match else "Calculating..."
                    eta_val = eta_match.group(1) if eta_match else "--:--"
                    bar = get_progress_bar(pct_val)

                    try:
                        await status_msg.edit_text(
                            f"⚡ **PRO LECTURE ENGINE — DOWNLOADING**\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎬 **Title:** `{safe_title}`\n\n"
                            f"📊 **Progress:** [{bar}] `{pct_val:.1f}%`\n"
                            f"🚀 **Speed:** `{spd_val}` *(32x Threads)*\n"
                            f"⏳ **ETA:** `{eta_val}`\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━",
                            reply_markup=cancel_Btn
                        )
                    except Exception:
                        pass

        await process.wait()

        if task_id in CANCELLED_TASKS:
            return

        if not os.path.exists(output_mp4):
            err_tail = "\n".join(error_logs[-5:]) if error_logs else "Unknown Error"
            await status_msg.edit_text(
                f"❌ **Download Failed!** *(Aapki daily limit cut nahi hui hai)*\n\n**Reason (Logs):**\n`{err_tail}`"
            )
            return

        await status_msg.edit_text(
            "🛠️ **Processing Video Metadata & 16:9 Aspect Ratio...**"
        )

        width, height, duration = await get_video_metadata(output_mp4)
        file_size = os.path.getsize(output_mp4)

        try:
            thumb_time = "00:00:05" if duration > 6 else "00:00:01"
            thumb_cmd = [
                "ffmpeg", "-y", "-ss", thumb_time, "-i", output_mp4,
                "-vframes", "1", "-vf", f"scale={width}:{height}",
                "-q:v", "2", thumb_jpg
            ]
            t_proc = await asyncio.create_subprocess_exec(
                *thumb_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await t_proc.wait()
        except Exception:
            pass

        start_up = time.time()
        total_time = format_time(time.time() - task_start_time)

        # ✅ VIDEO SUCCESSFULLY READY -> UPDATE USER COUNT IN DATABASE
        updated_user = increment_user_download(user)
        new_quota_str = "♾️ Unlimited (Admin)" if is_admin else f"{updated_user['today_count']}/{DAILY_LIMIT} Used Today"

        caption_text = (
            f"🎓 **{title}**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎞️ **Resolution:** `{width}x{height} (16:9 HD)`\n"
            f"📦 **File Size:** `{humanbytes(file_size)}`\n"
            f"⏱️ **Duration:** `{format_time(duration)}`\n"
            f"⚡ **Processed In:** `{total_time}`\n"
            f"📊 **Daily Quota:** `{new_quota_str}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *Powered by High-Speed Lecture Engine*"
        )

        await client.send_video(
            chat_id=message.chat.id,
            video=output_mp4,
            caption=caption_text,
            duration=duration,
            width=width,
            height=height,
            thumb=thumb_jpg if os.path.exists(thumb_jpg) else None,
            supports_streaming=True,
            progress=upload_progress,
            progress_args=(status_msg, start_up, title, task_id)
        )

        await status_msg.delete()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        if task_id not in CANCELLED_TASKS:
            await status_msg.edit_text(f"❌ **System Error:** `{str(e)}`")

    finally:
        ACTIVE_PROCESSES.pop(task_id, None)
        CANCELLED_TASKS.discard(task_id)
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)

print("🚀 Starting Pro Lecture Engine v3.0 (With User Analytics & Daily Limits)...")
bot.run()
