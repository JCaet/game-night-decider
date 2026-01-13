"""Fix handlers.py to add Cancel button and update Poll button emoji."""

with open("src/bot/handlers.py", encoding="utf-8") as f:
    content = f.read()

# Fix any remaining old poll buttons to use blue circle
# The chart emoji might render differently on different systems

# Replace patterns for Poll button lines that don't have Cancel button after
import re

# Pattern 1: Find keyboard sections with Poll but no Cancel
# Looking for the pattern where Poll is followed immediately by closing bracket

# First, find all places where we need to add Cancel button
# Pattern: [InlineKeyboardButton(...Poll...)] followed by \n    ] (end of keyboard array)

# Match the last Poll button in a keyboard array that doesn't have Cancel
lines = content.split("\n")
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    # Check if this is a Poll button line (any emoji variant)
    if "Poll" in line and 'callback_data="start_poll"' in line:
        # Make sure it uses the blue circle emoji
        line = re.sub(r'"\S* Poll"', '"🔵 Poll"', line)
        # Check if next line is ]    (end of keyboard)
        if i + 1 < len(lines) and lines[i + 1].strip() == "]":
            # Add comma to the poll line if not present
            if not line.rstrip().endswith(","):
                line = line.rstrip() + ","
            new_lines.append(line)
            # Add cancel button before the closing ]
            indent = len(line) - len(line.lstrip())
            new_lines.append(
                " " * indent + '[InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")]'
            )
            i += 1
            continue
    new_lines.append(line)
    i += 1

content = "\n".join(new_lines)

with open("src/bot/handlers.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done! Updated Poll buttons and added Cancel buttons where missing.")
