import keyboard
import webbrowser
from datetime import datetime
from pathlib import Path

BOARDMEETING_URL = "http://localhost:8000"
LOG_FILE = Path(__file__).with_name("BoardMeeting_Notes.txt")


def open_boardmeeting():
    webbrowser.open(BOARDMEETING_URL)
    print("Opened BoardMeeting:", BOARDMEETING_URL)


def add_note():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = input("\n[Hotkey note] Type note and press Enter: ")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now}] {text}\n")
    print("Saved note to", LOG_FILE)


print("Hotkeys ON (for this window only):")
print("  Ctrl+Alt+B -> open BoardMeeting")
print("  Ctrl+Alt+N -> add note to BoardMeeting_Notes.txt")
print("  Esc        -> quit this helper\n")

keyboard.add_hotkey("ctrl+alt+b", open_boardmeeting)
keyboard.add_hotkey("ctrl+alt+n", add_note)

keyboard.wait("esc")

print("Hotkeys OFF. Exiting bm_hotkeys.py.")
