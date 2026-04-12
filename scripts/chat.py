#!/usr/bin/env python3
"""
Stateful chat interface for subagents.

Starts cli_bot.py in background, communicates via files.
Bot reads from /tmp/eventai_input, writes to /tmp/eventai_output.

Usage:
    # First call starts the bot:
    python scripts/chat.py "сообщение"
    python scripts/chat.py "@role:guest:student"   # button
    python scripts/chat.py "!state"                # check state
    python scripts/chat.py "!reset"                # kill & restart

Each call sends ONE message and prints the bot's response.
Session persists between calls.
"""

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

INPUT_FILE = "/tmp/eventai_input"
OUTPUT_FILE = "/tmp/eventai_output"
PID_FILE = "/tmp/eventai_bot.pid"
LOCK_FILE = "/tmp/eventai_bot.lock"
TIMEOUT = 60

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _is_running() -> bool:
    if not os.path.exists(PID_FILE):
        return False
    with open(PID_FILE) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        os.unlink(PID_FILE)
        return False


def _kill():
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.unlink(PID_FILE)
    for f in [INPUT_FILE, OUTPUT_FILE]:
        if os.path.exists(f):
            os.unlink(f)


def _start():
    """Start the bot wrapper in background."""
    _kill()  # clean up any previous session

    # Create the wrapper script that bridges files <-> cli_bot stdin/stdout
    wrapper = f"""
import asyncio, os, sys, time
sys.path.insert(0, '{PROJECT_ROOT}')
os.environ.setdefault('BOT_TOKEN', 'test')

INPUT = '{INPUT_FILE}'
OUTPUT = '{OUTPUT_FILE}'

# Create empty files
open(INPUT, 'w').close()
open(OUTPUT, 'w').close()

from scripts.cli_bot import CLIBot, setup_dispatcher, make_message, make_callback, _display_pipe

async def run():
    bot = CLIBot()
    dp = await setup_dispatcher(bot)
    await dp.emit_startup()

    # Signal ready
    with open(OUTPUT, 'w') as f:
        f.write('READY\\n')
        f.flush()

    last_pos = 0
    msg_counter = 0

    while True:
        await asyncio.sleep(0.2)

        # Check for new input
        try:
            with open(INPUT, 'r') as f:
                content = f.read()
        except FileNotFoundError:
            break

        if len(content) <= last_pos:
            continue

        new_input = content[last_pos:].strip()
        last_pos = len(content)

        if not new_input:
            continue

        for line in new_input.split('\\n'):
            line = line.strip()
            if not line:
                continue

            if line == '!quit':
                await dp.emit_shutdown()
                return

            if line == '!state':
                state = dp.fsm.get_context(bot, user_id=777, chat_id=777)
                current = await state.get_state()
                with open(OUTPUT, 'a') as f:
                    f.write(f'STATE: {{current or "(none)"}}\\n')
                    f.write('DONE\\n')
                    f.flush()
                continue

            msg_counter += 1

            if line.startswith('@'):
                from scripts.cli_bot import make_callback
                update = make_callback(line[1:], msg_counter)
            else:
                from scripts.cli_bot import make_message
                update = make_message(line, msg_counter)

            try:
                await dp.feed_update(bot, update)
            except Exception as e:
                with open(OUTPUT, 'a') as f:
                    f.write(f'ERROR: {{e}}\\n')
                    f.write('DONE\\n')
                    f.flush()
                continue

            # Capture responses
            responses = []
            for method in bot.drain_messages():
                from aiogram.methods import SendMessage, EditMessageText, AnswerCallbackQuery
                if isinstance(method, SendMessage):
                    responses.append(f'BOT: {{method.text or ""}}')
                    if method.reply_markup and hasattr(method.reply_markup, 'inline_keyboard'):
                        for row in method.reply_markup.inline_keyboard:
                            for btn in row:
                                responses.append(f'BUTTON: [{{btn.text}}] @{{btn.callback_data}}')
                elif isinstance(method, EditMessageText):
                    responses.append(f'BOT_EDIT: {{method.text or ""}}')
                    if method.reply_markup and hasattr(method.reply_markup, 'inline_keyboard'):
                        for row in method.reply_markup.inline_keyboard:
                            for btn in row:
                                responses.append(f'BUTTON: [{{btn.text}}] @{{btn.callback_data}}')
                elif isinstance(method, AnswerCallbackQuery):
                    if method.text:
                        responses.append(f'POPUP: {{method.text}}')

            with open(OUTPUT, 'a') as f:
                for r in responses:
                    f.write(r + '\\n')
                f.write('DONE\\n')
                f.flush()

    await dp.emit_shutdown()

asyncio.run(run())
"""
    env = os.environ.copy()
    env["BOT_TOKEN"] = "test"

    proc = subprocess.Popen(
        ["python3.12", "-c", wrapper],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    # Wait for READY
    for _ in range(60):
        time.sleep(0.5)
        if os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE) as f:
                if "READY" in f.read():
                    return
    raise RuntimeError("Bot failed to start within 30s")


def _send(text: str) -> str:
    """Send message and wait for response."""
    # Lock to prevent concurrent access
    lock_fd = open(LOCK_FILE, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)

    try:
        # Read current output length
        with open(OUTPUT_FILE) as f:
            before = f.read()
        before_len = len(before)

        # Append input
        with open(INPUT_FILE, "a") as f:
            f.write(text + "\n")
            f.flush()

        # Wait for DONE marker in output
        for _ in range(TIMEOUT * 5):
            time.sleep(0.2)
            with open(OUTPUT_FILE) as f:
                content = f.read()
            new_content = content[before_len:]
            if "DONE" in new_content:
                # Extract response (everything before DONE)
                lines = new_content.split("\n")
                result = []
                for line in lines:
                    if line.strip() == "DONE":
                        break
                    if line.strip():
                        result.append(line)
                return "\n".join(result)

        return "TIMEOUT: No response within 60s"
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/chat.py <message>")
        print('  python scripts/chat.py "/start"')
        print('  python scripts/chat.py "@role:guest:student"')
        print('  python scripts/chat.py "!state"')
        print('  python scripts/chat.py "!reset"')
        sys.exit(1)

    text = " ".join(sys.argv[1:])

    if text == "!reset":
        _kill()
        print("Session reset.")
        return

    if not _is_running():
        print("Starting bot...", flush=True)
        _start()
        print("Bot ready.", flush=True)

    response = _send(text)
    print(response)


if __name__ == "__main__":
    main()
