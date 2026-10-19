import os
import re
import sys
import time
import math
import json
import shutil
import asyncio
from urllib.parse import urlparse, parse_qs, unquote
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# ================= SECRETS CHECK =================
API_ID_STR = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

if not API_ID_STR or not API_HASH or not BOT_TOKEN:
    print("❌ ERROR: GitHub Secrets (API_ID, API_HASH, BOT_TOKEN) खाली हैं!")
    sys.exit(1)

API_ID = int(API_ID_STR)

bot = Client(
    "pro_uploader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    workers=16,
    max_concurrent_transmissions=8  # Upload speed boost ke liye parallel connections
)

# Active downloads track karne ke liye (Cancel feature)
ACTIVE_PROCESSES = {}
CANCELLED_TASKS = set()

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
    """Video ka exact Width, Height aur Duration nikalta hai (Portrait bug fix)"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        file_path
    ]
    width, height, duration = 1280, 720, 0
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
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

# ================= BOT HANDLERS =================
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    await message.reply_text(
        "⚡ **SPIDY PRO LECTURE DOWNLOADER v2.0** ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 **Engine:** `32x Multi-Thread Turbo`\n"
        "📺 **Aspect Ratio:** `Auto 16:9 Landscape Fix`\n"
        "🛡️ **Bypass:** `Akamai / AppX Ready`\n\n"
        "📝 **Kaise Use Karein:**\n"
        "`https://link.m3u8 | Lecture 01 - Physics`\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

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
        await callback_query.message.edit_text("🛑 **Task Cancelled by User!**")
    except Exception:
        pass

@bot.on_message(filters.private & filters.text & ~filters.command(["start"]))
async def handle_link(client, message: Message):
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

    status_msg = await message.reply_text(
        f"⚡ **INITIALIZING 32X TURBO ENGINE...**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 **Title:** `{safe_title}`\n"
        f"🔗 **Status:** `Connecting to Akamai CDN...`",
        reply_markup=cancel_Btn
    )

    task_start_time = time.time()

    try:
        # 🚀 32x Concurrent Fragments + 10M HTTP Chunking for Maximum Speed
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
                f"❌ **Download Failed!**\n\n**Reason (Logs):**\n`{err_tail}`"
            )
            return

        await status_msg.edit_text(
            "🛠️ **Processing Video Metadata & 16:9 Aspect Ratio...**"
        )

        # ✅ FIX PORTRAIT BUG: Extract Exact Width, Height & Duration
        width, height, duration = await get_video_metadata(output_mp4)
        file_size = os.path.getsize(output_mp4)

        # Generate High-Res Landscape Thumbnail matching video dimensions
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

        caption_text = (
            f"🎓 **{title}**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎞️ **Resolution:** `{width}x{height} (16:9 HD)`\n"
            f"📦 **File Size:** `{humanbytes(file_size)}`\n"
            f"⏱️ **Duration:** `{format_time(duration)}`\n"
            f"⚡ **Processed In:** `{total_time}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *Powered by High-Speed Lecture Engine*"
        )

        await client.send_video(
            chat_id=message.chat.id,
            video=output_mp4,
            caption=caption_text,
            duration=duration,
            width=width,    # 👈 Yeh line Portrait bug ko 100% theek karti hai
            height=height,  # 👈 Video exact YouTube/Landscape size me dikhega
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

print("🚀 Starting Pro Lecture Engine v2.0...")
bot.run()
