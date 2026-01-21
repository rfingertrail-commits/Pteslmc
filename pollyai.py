#!/usr/bin/env python3
import os
import sys
import subprocess
import tempfile
import threading
import time
import requests
import logging
import telebot
import pty
import select

from datetime import datetime
from pathlib import Path
from io import BytesIO

# ================= CONFIG =================
BOT_TOKEN = "7214966757:AAHwwQZQst5_ei1gkuyt9MHzNdJT66PlVZ8"
ADMIN_ID = 7431622335
POLLINATIONS_API = "https://image.pollinations.ai/prompt/"
# =========================================

bot = telebot.TeleBot(BOT_TOKEN)
user_processes = {}
state_lock = threading.Lock()

# Set up logging for Railway
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("pollyai")

# ================= UTIL =================

def is_admin(uid):
    return uid == ADMIN_ID

def output_dir():
    d = Path(tempfile.gettempdir()) / "telegram_bot"
    d.mkdir(exist_ok=True, mode=0o700)
    return d

# ================= IMAGE FUNCTIONS =================

def generate_image_url(prompt, width=512, height=512):
    prompt = prompt.replace(" ", "%20")
    seed = int(time.time() * 1000) % 10000
    return f"{POLLINATIONS_API}{prompt}?width={width}&height={height}&seed={seed}&nologo=true"

def download_image(url):
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return BytesIO(r.content)
    except Exception as e:
        logger.error(f"Image download failed: {e}")
        return None

# ================= START / HELP =================

@bot.message_handler(commands=["start", "help"])
def start_cmd(message):
    bot.reply_to(
        message,
        "🎨 AI Image Generator Bot\n\n"
        "Commands:\n"
        "• /prompt <text> – generate image\n"
        )

# ================= IMAGE COMMAND =================

@bot.message_handler(commands=["prompt"])
def prompt_cmd(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ Usage: /prompt <description>")
        return

    prompt = parts[1]
    msg = bot.reply_to(message, "🎨 Generating image...")

    url = generate_image_url(prompt)
    img = download_image(url)

    if not img:
        bot.edit_message_text("❌ Failed to generate image", message.chat.id, msg.message_id)
        return

    bot.send_photo(
        message.chat.id,
        img,
        caption=f"🎨 {prompt}",
        reply_to_message_id=message.message_id
    )
    bot.delete_message(message.chat.id, msg.message_id)

# ================= COMMAND EXECUTION =================

INTERACTIVE_KEYWORDS = (
    "ssh", "sshx", "tail -f", "ping",
    "watch", "htop", "top", "nano", "vim"
)

def is_interactive(cmd):
    return any(k in cmd for k in INTERACTIVE_KEYWORDS)

# ---------- SIMPLE COMMAND ----------

def run_simple(command, out_file, chat_id, msg_id, uid):
    p = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    with open(out_file, "w") as f:
        f.write("===  EXECUTION ===\n")
        f.write(f"Time: {datetime.now()}\n")
        f.write(f"D: {p.pid}\n")
        f.write("=" * 40 + "\n\n")

    def reader():
        for line in p.stdout:
            with open(out_file, "a") as f:
                f.write(line)

        code = p.wait()
        with open(out_file, "a") as f:
            f.write("\n" + "=" * 40 + "\n")
            f.write(f"Exit Code: {code}\n")

        bot.edit_message_text(
            f"✅  finished (exit {code})",
            chat_id, msg_id
        )

        with state_lock:
            user_processes.pop(uid, None)

    threading.Thread(target=reader, daemon=True).start()
    return p

# ---------- INTERACTIVE (FIXED) ----------

def run_interactive(command, out_file, chat_id, msg_id, uid):
    master, slave = pty.openpty()

    p = subprocess.Popen(
        command,
        shell=True,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True
    )
    os.close(slave)

    with open(out_file, "w") as f:
        f.write("=== INTERACTIVE  ===\n")
        f.write(f"Time: {datetime.now()}\n")
        f.write(f"D: {p.pid}\n")
        f.write("=" * 40 + "\n\n")

    def reader():
        try:
            while True:
                if p.poll() is not None:
                    break
                r, _, _ = select.select([master], [], [], 0.5)
                if master in r:
                    data = os.read(master, 4096)
                    if not data:
                        break
                    with open(out_file, "a") as f:
                        f.write(data.decode(errors="replace"))
        finally:
            os.close(master)
            with state_lock:
                user_processes.pop(uid, None)

    threading.Thread(target=reader, daemon=True).start()

    bot.edit_message_text(
        f"🟢 InteD: {p.pid}",
        chat_id, msg_id
    )

    return p

# ================= RUN =================

@bot.message_handler(commands=["run"])
def run_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ No ")
        return

    command = parts[1]
    msg = bot.reply_to(message, "⏳ Executing ...")

    with state_lock:
        if uid in user_processes:
            bot.edit_message_text("❌ already.", 
                                 message.chat.id, msg.message_id)
            return

    out = output_dir() / f"cmd_{int(time.time())}.txt"

    try:
        if is_interactive(command):
            p = run_interactive(command, out, message.chat.id, msg.message_id, uid)
        else:
            p = run_simple(command, out, message.chat.id, msg.message_id, uid)
    except Exception as e:
        bot.edit_message_text(f"❌ Error starting process: {e}", 
                             message.chat.id, msg.message_id)
        return

    with state_lock:
        user_processes[uid] = {"process": p, "output": out}

# ================= OUTPUT =================

@bot.message_handler(commands=["output"])
def output_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    with state_lock:
        data = user_processes.get(uid)

    if not data or not data["output"].exists():
        bot.reply_to(message, "❌ No output available")
        return

    try:
        with open(data["output"], "rb") as f:
            bot.send_document(message.chat.id, f, visible_file_name="output.txt")
    except Exception as e:
        bot.reply_to(message, f"❌ Error reading output: {e}")

# ================= STOP =================

@bot.message_handler(commands=["stop"])
def stop_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    with state_lock:
        data = user_processes.get(uid)
        if not data:
            bot.reply_to(message, "❌ No running process found")
            return
        p = data["process"]

    try:
        p.terminate()
        time.sleep(0.5)
        if p.poll() is None:
            p.kill()
    except Exception as e:
        logger.error(f"Error stopping process: {e}")

    with state_lock:
        user_processes.pop(uid, None)

    bot.reply_to(message, "🛑 Process stopped")

# ================= STATUS =================

@bot.message_handler(commands=["status"])
def status_cmd(message):
    uid = message.from_user.id
    if not is_admin(uid):
        return

    with state_lock:
        data = user_processes.get(uid)

    if not data:
        bot.reply_to(message, "📭 No active process")
    else:
        pid = data['process'].pid
        status = "running" if data['process'].poll() is None else "exited"
        bot.reply_to(message, f"🔄 ID: {pid}\nStatus: {status}")

# ================= START BOT =================

if __name__ == "__main__":
    logger.info("Starting PollyAI Bot...")
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30)
    except Exception as e:
        logger.error(f"Bot stopped with error: {e}")
        sys.exit(1)
