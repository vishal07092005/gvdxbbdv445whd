import os
import re
import sys
import time
import math
import shutil
import asyncio
from urllib.parse import urlparse, parse_qs, unquote
from pyrogram import Client, filters
from pyrogram.types import Message

# Secrets Check
API_ID_STR = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

if not API_ID_STR or not API_HASH or not BOT_TOKEN:
    print("❌ ERROR: GitHub Secrets (API_ID, API_HASH, BOT_TOKEN) खाली हैं! Settings > Secrets > Actions में जाकर चेक करो।")
    sys.exit(1)

API_ID = int(API_ID_STR)

bot = Client(
    "fast_uploader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

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

def extract_real_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    qs = parse_qs(parsed.query)
    if "url" in qs:
        return unquote(qs["url"][0])
    return raw_url

async def upload_progress(current, total, status_msg: Message, start_time, title):
    now = time.time()
    diff = now - start_time
    if round(diff % 3.00) == 0 or current == total:
        percentage = current * 100 / total
        speed = current / diff if diff > 0 else 0
        eta = round((total - current) / speed) if speed > 0 else 0
        
        filled = math.floor(percentage / 10)
        bar = "█" * filled + "░" * (10 - filled)
        
        text = (
            f"📤 **Uploading to Telegram...**\n\n"
            f"🎬 **Title:** `{title}`\n"
            f"📊 **Progress:** [{bar}] `{percentage:.1f}%`\n"
            f"📦 **Size:** `{humanbytes(current)}` / `{humanbytes(total)}`\n"
            f"🚀 **Speed:** `{humanbytes(speed)}/s`\n"
            f"⏳ **ETA:** `{eta}s`"
        )
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message: Message):
    await message.reply_text(
        "🔥 **Superfast Lecture Downloader Ready!**\n\n"
        "Seedha link bhejein ya naam ke sath:\n"
        "`<link> | Lecture Name`"
    )

@bot.on_message(filters.private & filters.text & ~filters.command(["start"]))
async def handle_link(client, message: Message):
    text = message.text.strip()
    if not text.startswith("http"):
        await message.reply_text("❌ Bhai valid link bhej (`http` ya `https` se shuru hone wala).")
        return

    if "|" in text:
        raw_url, title = [x.strip() for x in text.split("|", 1)]
    else:
        parts = text.split(maxsplit=1)
        raw_url = parts[0]
        title = parts[1] if len(parts) > 1 else f"Lecture_{message.id}"

    safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or f"Lecture_{message.id}"
    m3u8_url = extract_real_url(raw_url)

    work_dir = f"/tmp/dl_{message.id}"
    os.makedirs(work_dir, exist_ok=True)
    output_mp4 = os.path.join(work_dir, f"{safe_title}.mp4")
    thumb_jpg = os.path.join(work_dir, "thumb.jpg")

    status_msg = await message.reply_text(
        f"🔍 **Link Extracted!**\n🎬 **Title:** `{safe_title}`\n⚡ **Starting 16x Turbo Download...**"
    )

    try:
        # ✅ FIX 1 & 2: Added Akamai Bypass Headers & Removed conflicting aria2c flags for HLS
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--newline",
            "--no-check-certificate",
            "-N", "16",  # 16 chunks ek sath download karega
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

        last_update = 0
        error_logs = []  # ✅ FIX 3: Last kuch lines save karega taaki asli error dikhe

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="ignore").strip()
            if line_str:
                error_logs.append(line_str)
                if len(error_logs) > 10:
                    error_logs.pop(0)

            if "[download]" in line_str or "%" in line_str:
                now = time.time()
                if now - last_update >= 3:
                    last_update = now
                    try:
                        await status_msg.edit_text(
                            f"⚡ **Downloading & Merging TS Chunks (16x Speed)...**\n\n"
                            f"🎬 **Title:** `{safe_title}`\n"
                            f"📈 **Live Status:**\n`{line_str}`"
                        )
                    except Exception:
                        pass

        await process.wait()

        if not os.path.exists(output_mp4):
            err_tail = "\n".join(error_logs[-5:]) if error_logs else "Unknown Error"
            await status_msg.edit_text(
                f"❌ **Download Failed!**\n\n**Reason (Logs):**\n`{err_tail}`"
            )
            return

        await status_msg.edit_text("🖼️ **Generating Thumbnail & Extracting Duration...**")

        duration = 0
        try:
            dur_cmd = [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", output_mp4
            ]
            dur_out = await asyncio.create_subprocess_exec(*dur_cmd, stdout=asyncio.subprocess.PIPE)
            stdout, _ = await dur_out.communicate()
            duration = int(float(stdout.decode().strip()))
        except Exception:
            duration = 0

        try:
            thumb_cmd = [
                "ffmpeg", "-y", "-ss", "00:00:05", "-i", output_mp4,
                "-vframes", "1", "-q:v", "2", thumb_jpg
            ]
            t_proc = await asyncio.create_subprocess_exec(
                *thumb_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await t_proc.wait()
        except Exception:
            pass

        start_up = time.time()
        await client.send_video(
            chat_id=message.chat.id,
            video=output_mp4,
            caption=f"📚 **{title}**\n\n✅ *Downloaded via High-Speed Bot*",
            duration=duration,
            thumb=thumb_jpg if os.path.exists(thumb_jpg) else None,
            supports_streaming=True,
            progress=upload_progress,
            progress_args=(status_msg, start_up, title)
        )

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ **Error:** `{str(e)}`")

    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)

print("🚀 Starting High-Speed Bot...")
bot.run()
