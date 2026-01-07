"""
keyboard_setup_and_test.py

Run with:
    py keyboard_setup_and_test.py
"""

import keyboard

print("Press SPACE to say hi, press ESC to quit.")

while True:
    if keyboard.is_pressed("space"):
        print("Hi from keyboard module!")
        keyboard.wait("space")  # wait for release so it doesn't spam
    if keyboard.is_pressed("esc"):
        print("Exiting.")
        break
