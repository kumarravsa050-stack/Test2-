import os
import re
import time
import math
import glob
import json
import shutil
import asyncio
import subprocess
from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from pyrogram.errors import FloodWait
import yt_dlp
from keep_alive import keep_alive

# ==================== CONFIGURATION ====================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = 8275826746  # Your Telegram Owner ID hardcoded

app = Client("yt_clean_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COOKIES_FILE = "youtube_cookies.txt"
RENDER_SECRET_COOKIES = "/etc/secrets/youtube_cookies.txt"
MAX_SIZE_BYTES = int(1.9 * 1024 * 1024 * 1024)  # 1.9 GB safe split threshold

AUTHORIZED_USERS = {OWNER_ID} if OWNER_ID else set()
USER_SETTINGS = {}
PENDING_LINKS = {}
ACTIVE_TASKS = {}


# ==================== HELPER UTILITIES ====================

def is_authorized(user_id: int) -> bool:
    if not OWNER_ID:
        return True
    return user_id in AUTHORIZED_USERS or user_id == OWNER_ID


def get_user_setting(user_id: int, key: str, default: str = "") -> str:
    return USER_SETTINGS.get(user_id, {}).get(key, default)


def set_user_setting(user_id: int, key: str, value: str):
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = {"quality": "720", "custom_caption": ""}
    USER_SETTINGS[user_id][key] = value


def get_active_cookie_file() -> str | None:
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0:
        return COOKIES_FILE
    if os.path.exists(RENDER_SECRET_COOKIES) and os.path.getsize(RENDER_SECRET_COOKIES) > 0:
        shutil.copy(RENDER_SECRET_COOKIES, COOKIES_FILE)
        return COOKIES_FILE
    return None


def human_bytes(size: float) -> str:
    if not size:
        return "0 B"
    power = 1024
    n = 0
    units = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB'}
    while size >= power and n < 3:
        size /= power
        n += 1
    return f"{size:.2f} {units[n]}"


def format_time(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render_progress_bar(percentage: float) -> str:
    filled = math.floor(percentage / 10)
    empty = 10 - filled
    return f"[{'█' * filled}{'░' * empty}] {percentage:.1f}%"


async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass


# ==================== FFMPEG METADATA, THUMBNAIL & 2GB SPLITTER ====================

def get_video_metadata(file_path: str) -> tuple[int, int, int]:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", file_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        data = json.loads(result.stdout)
        duration = int(float(data.get("format", {}).get("duration", 0)))
        width, height = 1280, 720
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = int(stream.get("width", 1280))
                height = int(stream.get("height", 720))
                break
        return duration, width, height
    except Exception:
        return 0, 1280, 720


def generate_thumbnail(video_path: str) -> str | None:
    thumb_path = f"{video_path}_thumb.jpg"
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-ss", "00:00:02", "-vframes", "1",
        "-vf", "scale=320:-1", thumb_path
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return thumb_path if os.path.exists(thumb_path) else None


def split_large_video(file_path: str) -> list:
    file_size = os.path.getsize(file_path)
    if file_size <= MAX_SIZE_BYTES:
        return [file_path]

    duration, _, _ = get_video_metadata(file_path)
    if duration <= 0:
        return [file_path]

    num_parts = math.ceil(file_size / MAX_SIZE_BYTES)
    segment_time = math.ceil(duration / num_parts)

    base_name, ext = os.path.splitext(file_path)
    output_pattern = f"{base_name}_part%03d{ext}"

    cmd = [
        "ffmpeg", "-y", "-i", file_path, "-c", "copy", "-map", "0",
        "-segment_time", str(segment_time), "-f", "segment",
        "-reset_timestamps", "1", output_pattern
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.remove(file_path)

    return sorted(glob.glob(f"{base_name}_part*{ext}"))


# ==================== PROGRESS HOOKS ====================

async def pyrogram_upload_progress(current: int, total: int, status_msg: Message, start_time: float, last_update: dict, title: str):
    now = time.time()
    if (now - last_update["time"]) < 4 and current != total:
        return
    last_update["time"] = now
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    percentage = (current * 100 / total) if total > 0 else 0
    eta = ((total - current) / speed) if speed > 0 else 0

    lines = [
        "📤 **Uploading to Telegram...**",
        f"🎬 `{title[:55]}`",
        "",
        f"{render_progress_bar(percentage)}",
        f"📦 **Uploaded:** `{human_bytes(current)} / {human_bytes(total)}`",
        f"⚡ **Speed:** `{human_bytes(speed)}/s` | ⏳ **ETA:** `{format_time(eta)}`",
        "",
        "🛑 Send `/stop` to cancel."
    ]
    await safe_edit(status_msg, "\n".join(lines))


def make_ytdlp_hook(loop, status_msg: Message, last_update: dict, user_id: int):
    def hook(d):
        if not ACTIVE_TASKS.get(user_id, True):
            raise Exception("Download cancelled by user via /stop.")

        if d['status'] == 'downloading':
            now = time.time()
            if (now - last_update["time"]) < 4:
                return
            last_update["time"] = now

            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            speed = d.get('speed') or 0
            eta = d.get('eta') or 0
            filename = os.path.basename(d.get('filename', 'Video'))
            percentage = (downloaded * 100 / total) if total > 0 else 0

            lines = [
                "📥 **Downloading from YouTube...**",
                f"🎬 `{filename[:55]}`",
                "",
                f"{render_progress_bar(percentage)}",
                f"📦 **Size:** `{human_bytes(downloaded)} / {human_bytes(total)}`",
                f"⚡ **Speed:** `{human_bytes(speed)}/s` | ⏳ **ETA:** `{format_time(eta)}`",
                "",
                "🛑 Send `/stop` to cancel."
            ]
            asyncio.run_coroutine_threadsafe(safe_edit(status_msg, "\n".join(lines)), loop)

        elif d['status'] == 'finished':
            text = "⚙️ **Merging video & audio streams with FFmpeg...**"
            asyncio.run_coroutine_threadsafe(safe_edit(status_msg, text), loop)

    return hook


# ==================== YOUTUBE DOWNLOAD ENGINE (FIXED FOR RENDER) ====================

def download_yt_media(url: str, quality: str, mode: str, user_dir: str, loop, status_msg: Message, user_id: int):
    last_update = {"time": 0.0}
    cookie_path = get_active_cookie_file()

    def build_opts(use_cookies: bool, clients: list):
        opts = {
            'outtmpl': os.path.join(user_dir, '%(title).75s.%(ext)s'),
            'merge_output_format': 'mp4',
            'noplaylist': (mode != 'playlist'),
            'quiet': True,
            'no_warnings': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'retries': 10,
            'fragment_retries': 10,
            'concurrent_fragment_downloads': 5,
            'extractor_args': {
                'youtube': {
                    'player_client': clients
                }
            },
            'progress_hooks': [make_ytdlp_hook(loop, status_msg, last_update, user_id)],
        }
        if use_cookies and cookie_path:
            opts['cookiefile'] = cookie_path

        if quality == 'audio' or mode == 'audio':
            opts['format'] = 'ba/b'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        elif mode == 'live':
            opts['live_from_start'] = True
        else:
            # Using format_sort instead of strict format filter so yt-dlp NEVER fails with "Requested format is not available"
            if quality.isdigit():
                opts['format_sort'] = [f'res:{quality}', 'ext:mp4:m4a']
            else:
                opts['format_sort'] = ['ext:mp4:m4a']
        return opts

    # Attempt 1: Multi-client with cookies (if present)
    try:
        opts1 = build_opts(use_cookies=True, clients=['tv', 'ios', 'mweb', 'web'])
        with yt_dlp.YoutubeDL(opts1) as ydl:
            ydl.download([url])
        return
    except Exception as e1:
        if "cancelled by user" in str(e1):
            raise e1

    # Attempt 2: Mobile clients without cookies (bypasses account/cookie SABR blocks)
    try:
        opts2 = build_opts(use_cookies=False, clients=['ios', 'android', 'tv'])
        with yt_dlp.YoutubeDL(opts2) as ydl:
            ydl.download([url])
        return
    except Exception as e2:
        if "cancelled by user" in str(e2):
            raise e2

    # Attempt 3: Pure default yt-dlp with no format restrictions at all
    opts3 = {
        'outtmpl': os.path.join(user_dir, '%(title).75s.%(ext)s'),
        'merge_output_format': 'mp4',
        'noplaylist': (mode != 'playlist'),
        'quiet': True,
        'no_warnings': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'progress_hooks': [make_ytdlp_hook(loop, status_msg, last_update, user_id)],
    }
    if cookie_path:
        opts3['cookiefile'] = cookie_path
    with yt_dlp.YoutubeDL(opts3) as ydl:
        ydl.download([url])


async def execute_download_and_upload(
    client: Client,
    message: Message,
    url: str,
    quality: str,
    mode: str,
    custom_title: str = "",
    index_prefix: str = ""
):
    user_id = message.from_user.id if message.from_user else message.chat.id
    ACTIVE_TASKS[user_id] = True

    prep_lines = [
        "⏳ **Preparing Download...**",
        f"🎚 **Quality:** `{quality}` | 🍪 **Cookies:** `{'Active' if get_active_cookie_file() else 'None'}`"
    ]
    status_msg = await message.reply_text("\n".join(prep_lines))
    user_dir = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(time.time())}")
    os.makedirs(user_dir, exist_ok=True)
    loop = asyncio.get_running_loop()

    try:
        await asyncio.to_thread(download_yt_media, url, quality, mode, user_dir, loop, status_msg, user_id)

        downloaded_files = [
            f for f in os.listdir(user_dir)
            if not f.endswith(('.part', '.ytdl', '.jpg'))
        ]
        if not downloaded_files:
            await safe_edit(status_msg, "❌ **Download failed.** No media files found.")
            return

        custom_tag = get_user_setting(user_id, "custom_caption", "")

        for file_name in sorted(downloaded_files):
            if not ACTIVE_TASKS.get(user_id, True):
                await safe_edit(status_msg, "🛑 **Task stopped by user.**")
                break

            full_path = os.path.join(user_dir, file_name)
            display_title = custom_title if custom_title else os.path.splitext(file_name)[0]

            if os.path.getsize(full_path) > MAX_SIZE_BYTES:
                await safe_edit(status_msg, f"✂️ **`{display_title}` exceeds 2GB.**\nSplitting losslessly with FFmpeg...")
                parts = await asyncio.to_thread(split_large_video, full_path)
            else:
                parts = [full_path]

            total_parts = len(parts)
            for idx, part_path in enumerate(parts, start=1):
                if not ACTIVE_TASKS.get(user_id, True):
                    break

                caption_lines = []
                if index_prefix:
                    caption_lines.append(f"📇 **Index:** `{index_prefix}`")
                caption_lines.append(f"🎬 **Title:** `{display_title}`")
                caption_lines.append(f"🎚 **Quality:** `{quality}`")
                if total_parts > 1:
                    caption_lines.append(f"📦 **Part:** `{idx} of {total_parts}`")
                if custom_tag:
                    caption_lines.append(f"\n✨ **Downloaded by:** {custom_tag}")
                caption = "\n".join(caption_lines)

                start_time = time.time()
                upload_state = {"time": 0.0}

                if part_path.endswith(".mp3"):
                    await client.send_audio(
                        chat_id=message.chat.id,
                        audio=part_path,
                        caption=caption,
                        progress=pyrogram_upload_progress,
                        progress_args=(status_msg, start_time, upload_state, display_title)
                    )
                else:
                    duration, width, height = await asyncio.to_thread(get_video_metadata, part_path)
                    thumb_path = await asyncio.to_thread(generate_thumbnail, part_path)

                    await client.send_video(
                        chat_id=message.chat.id,
                        video=part_path,
                        caption=caption,
                        duration=duration,
                        width=width,
                        height=height,
                        thumb=thumb_path,
                        supports_streaming=True,
                        progress=pyrogram_upload_progress,
                        progress_args=(status_msg, start_time, upload_state, display_title)
                    )
                    if thumb_path and os.path.exists(thumb_path):
                        os.remove(thumb_path)

                if os.path.exists(part_path):
                    os.remove(part_path)

        await status_msg.delete()

    except Exception as e:
        await safe_edit(status_msg, f"⚠️ **Alert:** `{str(e)[:350]}`")
    finally:
        shutil.rmtree(user_dir, ignore_errors=True)


# ==================== INTERACTIVE QUALITY MENU & CALLBACKS ====================

def get_quality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 360p", callback_data="dl_q|360"),
            InlineKeyboardButton("🎬 480p", callback_data="dl_q|480"),
        ],
        [
            InlineKeyboardButton("📺 720p HD", callback_data="dl_q|720"),
            InlineKeyboardButton("🔥 1080p FHD", callback_data="dl_q|1080"),
        ],
        [
            InlineKeyboardButton("🌟 Best Available", callback_data="dl_q|best"),
            InlineKeyboardButton("🎵 MP3 Audio", callback_data="dl_q|audio"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="dl_q|cancel")
        ]
    ])


@app.on_callback_query(filters.regex(r"^dl_q\|"))
async def quality_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    choice = query.data.split("|")[1]

    if choice == "cancel":
        PENDING_LINKS.pop(user_id, None)
        await query.message.edit_text("❌ **Download cancelled.**")
        return

    task_info = PENDING_LINKS.pop(user_id, None)
    if not task_info:
        await query.answer("⚠️ Session expired! Please send the link again.", show_alert=True)
        return

    await query.message.delete()
    await execute_download_and_upload(
        client=client,
        message=query.message,
        url=task_info["url"],
        quality=choice,
        mode=task_info["mode"]
    )


# ==================== BATCH .TXT & COOKIE FILE HANDLER ====================

@app.on_message(filters.document & filters.private)
async def handle_txt_or_cookies(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply_text("⛔ **Access Denied.** Contact the bot admin.")
        return

    file_name = message.document.file_name or ""

    if (message.caption and "/setcookies" in message.caption) or "cookie" in file_name.lower():
        if OWNER_ID and user_id != OWNER_ID:
            await message.reply_text("⛔ Only the Bot Owner can update `youtube_cookies.txt`.")
            return
        await client.download_media(message, file_name=os.path.join(os.getcwd(), COOKIES_FILE))
        await message.reply_text("✅ **`youtube_cookies.txt` uploaded and activated!**")
        return

    if file_name.endswith(".txt"):
        txt_path = await client.download_media(message, file_name=os.path.join(DOWNLOAD_DIR, f"{user_id}_batch.txt"))
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
        os.remove(txt_path)

        links = []
        url_pattern = re.compile(r'(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+)')
        for idx, line in enumerate(lines, start=1):
            match = url_pattern.search(line)
            if match:
                url = match.group(1)
                title_part = line[:match.start()].strip(" :-|") or f"Video_{idx:03d}"
                links.append((title_part, url))

        if not links:
            await message.reply_text("⚠️ No valid YouTube URLs found inside that `.txt` file.")
            return

        default_q = get_user_setting(user_id, "quality", "720")
        batch_lines = [
            "📄 **Batch `.txt` File Loaded!**",
            f"🔢 **Total YouTube Links:** `{len(links)}`",
            f"🎚 **Quality Setting:** `{default_q}p` *(Change via `/settings`)*",
            "",
            "🚀 *Starting sequential batch download... Send `/stop` anytime to abort.*"
        ]
        batch_intro = await message.reply_text("\n".join(batch_lines))

        ACTIVE_TASKS[user_id] = True
        for i, (title, url) in enumerate(links, start=1):
            if not ACTIVE_TASKS.get(user_id, True):
                await message.reply_text("🛑 **Batch `.txt` download stopped by user.**")
                break
            index_str = f"{i:03d}/{len(links):03d}"
            await execute_download_and_upload(
                client=client,
                message=message,
                url=url,
                quality=default_q,
                mode="video",
                custom_title=title,
                index_prefix=index_str
            )

        await batch_intro.reply_text("✅ **Batch `.txt` processing finished!**")


# ==================== BOT COMMANDS ====================

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    cookie_state = "✅ Active" if get_active_cookie_file() else "❌ Not Loaded"
    lines = [
        "👋 **Welcome to the All-in-One YouTube & `.txt` Batch Downloader Bot!**",
        "",
        "✨ **Core Features:**",
        "• **Any YouTube Link:** Send any Video, Shorts, Live, or Playlist URL to pick your resolution.",
        "• **Batch `.txt` Downloader:** Upload any `.txt` file containing multiple YouTube links (`Title : URL`) to download them all in order.",
        "• **Auto 2GB+ Splitter:** Videos over 1.9GB are automatically cut via FFmpeg and uploaded in parts with thumbnails.",
        "",
        "🛠 **Commands:**",
        "🔹 `/video <link>` | `/playlist <link>` | `/live <link>` | `/audio <link>`",
        "🔹 `/settings` - Set default `.txt` resolution & custom caption",
        "🔹 `/stop` - Cancel any running download or batch",
        "🔹 `/setcookies` - Upload `youtube_cookies.txt`",
        "🔹 `/add <id>` & `/remove <id>` - Authorize users (Admin)",
        "",
        f"🍪 **YouTube Cookies:** `{cookie_state}`"
    ]
    await message.reply_text("\n".join(lines))


@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    lines = [
        "📖 **How to Use This Bot:**",
        "",
        "1. **Single Video / Shorts / Playlist:** Paste any YouTube link directly into the chat and choose your desired resolution button.",
        "2. **Batch `.txt` Download:** Create a `.txt` file with lines formatted as `Title : https://youtu.be/...` and send the file here.",
        "3. **2GB+ Videos:** Any video larger than 1.9GB is automatically split into sequential parts using FFmpeg without losing quality.",
        "4. **YouTube Cookies:** If YouTube blocks a datacenter IP, send your exported `youtube_cookies.txt` file with the caption `/setcookies`."
    ]
    await message.reply_text("\n".join(lines))


@app.on_message(filters.command("stop"))
async def stop_cmd(client: Client, message: Message):
    ACTIVE_TASKS[message.from_user.id] = False
    await message.reply_text("🛑 **Stopping current download task...**")


@app.on_message(filters.command("settings"))
async def settings_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    q = get_user_setting(user_id, "quality", "720")
    cap = get_user_setting(user_id, "custom_caption", "None")
    lines = [
        "⚙️ **Your Bot Settings**",
        "",
        f"🎚 **Default `.txt` Batch Quality:** `{q}p`",
        f"📝 **Custom Caption Tag:** `{cap}`",
        "",
        "To change your default quality or caption, use:",
        "• `/setquality 720` *(Options: 360, 480, 720, 1080, best, audio)*",
        "• `/setcaption @YourName`"
    ]
    await message.reply_text("\n".join(lines))


@app.on_message(filters.command("setquality"))
async def set_quality_cmd(client: Client, message: Message):
    if len(message.command) < 2 or message.command[1] not in ("360", "480", "720", "1080", "best", "audio"):
        await message.reply_text("⚠️ **Usage:** `/setquality 720` *(360, 480, 720, 1080, best, audio)*")
        return
    set_user_setting(message.from_user.id, "quality", message.command[1])
    await message.reply_text(f"✅ Default `.txt` batch quality set to **`{message.command[1]}`**!")


@app.on_message(filters.command("setcaption"))
async def set_caption_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("⚠️ **Usage:** `/setcaption Your Custom Name`")
        return
    caption_val = message.text.split(None, 1)[1]
    set_user_setting(message.from_user.id, "custom_caption", caption_val)
    await message.reply_text(f"✅ Custom caption updated to: **`{caption_val}`**")


@app.on_message(filters.command("add"))
async def add_user_cmd(client: Client, message: Message):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply_text("⚠️ **Usage:** `/add <Telegram_User_ID>`")
        return
    uid = int(message.command[1])
    AUTHORIZED_USERS.add(uid)
    await message.reply_text(f"✅ User `{uid}` has been authorized.")


@app.on_message(filters.command("remove"))
async def remove_user_cmd(client: Client, message: Message):
    if OWNER_ID and message.from_user.id != OWNER_ID:
        return
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply_text("⚠️ **Usage:** `/remove <Telegram_User_ID>`")
        return
    uid = int(message.command[1])
    AUTHORIZED_USERS.discard(uid)
    await message.reply_text(f"🗑 User `{uid}` removed from authorized users.")


@app.on_message(filters.command(["video", "playlist", "audio", "live"]))
async def media_commands(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply_text("⛔ **Access Denied.**")
        return
    if len(message.command) < 2:
        await message.reply_text(f"⚠️ **Usage:** `/{message.command[0]} <YouTube_URL>`")
        return

    mode = message.command[0].lower()
    url = message.command[1].strip()

    if mode == "audio":
        await execute_download_and_upload(client, message, url, "audio", "audio")
    else:
        PENDING_LINKS[user_id] = {"url": url, "mode": mode}
        await message.reply_text(
            f"🎬 **Select Resolution for `{mode.upper()}`:**\n`{url}`",
            reply_markup=get_quality_keyboard()
        )


@app.on_message(filters.regex(r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+') & filters.private)
async def direct_link_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not is_authorized(user_id):
        await message.reply_text("⛔ **Access Denied.**")
        return

    url = message.text.strip()
    mode = "playlist" if "list=" in url else "video"
    PENDING_LINKS[user_id] = {"url": url, "mode": mode}

    await message.reply_text(
        f"🎬 **Select Video Quality:**\n`{url}`",
        reply_markup=get_quality_keyboard()
    )


if __name__ == "__main__":
    print("Starting Keep-Alive Flask Web Server...")
    keep_alive()
    print("Starting Clean YouTube Telegram Bot...")
    app.run()
