#!/usr/bin/env python3
"""Stand-in for the coding agent in the README demo. It is NOT Claude Code: no API call,
no tokens, no network. It prints a scripted answer, edits one file of the demo repo and reports
its state through src/hook.py, the same way the real Claude Code hooks do, so the workspace dots
and panels react as they would in real use. Its text is demo content, written in pt-BR.

The tool that "runs tests" only sleeps: it is there so the session shows the yellow dot
(a tool running for more than 8 s). Nothing is executed.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["WS_DIR"]) / "src"))
import i18n  # noqa: E402  (path set above)

HOOK = Path(os.environ["WS_DIR"]) / "src" / "hook.py"
CWD = os.getcwd()
SESSION = "demo-" + os.path.basename(CWD)
PAUSE = 0.6  # seconds between scripted lines (readable in the recording)

NEW_FUNCTION = '''

def clear_cart(items):
    """Empty the cart: return an empty list and a zero total."""
    items.clear()
    return items
'''


def hook(event, **extra):
    payload = {"hook_event_name": event, "session_id": SESSION, "cwd": CWD, **extra}
    subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                   text=True, timeout=5, check=False)


def say(text):
    print(text, flush=True)
    time.sleep(PAUSE)


def tool(name, detail, seconds=0.0, field="command"):
    print(f"⏺ {name}({detail})", flush=True)
    tool_input = {field: detail}
    hook("PreToolUse", tool_name=name, tool_input=tool_input)
    time.sleep(seconds)
    return tool_input


def answer(task):
    say(i18n.t("demo.fake.understood", task=task))
    tool("Read", "src/cart.py", 0.8, field="file_path")
    say(i18n.t("demo.fake.plan"))
    tool("Update", "src/cart.py", 0.0, field="file_path")
    path = Path(CWD) / "src" / "cart.py"
    path.write_text(path.read_text() + NEW_FUNCTION)
    hook("PostToolUse", tool_name="Update", tool_input={"file_path": "src/cart.py"})
    tool("Bash", "python3 -m pytest -q tests/test_cart.py", 12.0)   # longer than 8 s: the dot turns yellow
    hook("PostToolUse", tool_name="Bash", tool_input={"command": "pytest"})
    print("   3 passed", flush=True)
    time.sleep(PAUSE)
    say(i18n.t("demo.fake.done"))


def main():
    print("\033[H\033[2J\033[3J", end="")   # clear the shell's launch line (it shows the demo's temp path)
    hook("SessionStart")
    print(i18n.t("demo.fake.banner"), flush=True)
    while True:
        print("> ", end="", flush=True)
        try:
            task = input()
        except EOFError:
            break
        if not task.strip():
            continue
        hook("UserPromptSubmit", prompt=task)
        answer(task)
        hook("Stop")


if __name__ == "__main__":
    main()
