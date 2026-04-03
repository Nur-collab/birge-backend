import sys

with open("main.py", "r", encoding="utf-8") as f:
    text = f.read()

target = '            "time": driver_trip.time if driver_trip else "",\n'
replacement = '            "time": driver_trip.time if driver_trip else "",\n            "date": driver_trip.date if driver_trip else "",\n'

if target in text:
    text = text.replace(target, replacement)
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(text)
    print("Patched successfully")
else:
    print("Target not found")
