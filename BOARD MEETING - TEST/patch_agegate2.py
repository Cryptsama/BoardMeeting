from pathlib import Path
import re

p = Path("app.py")
s = p.read_text(encoding="utf-8-sig")

# add import (once)
if "from agegate_routes import setup_agegate" not in s:
    # put it near other imports; safest is right after Jinja2Templates import
    s = s.replace(
        "from fastapi.templating import Jinja2Templates\n",
        "from fastapi.templating import Jinja2Templates\nfrom agegate_routes import setup_agegate\n",
        1
    )

# add setup call (once), right after templates = Jinja2Templates(...)
m = re.search(r"^templates\s*=\s*Jinja2Templates\(.*\)\s*$", s, flags=re.M)
if m and "setup_agegate(app, templates)" not in s:
    insert_at = m.end()
    s = s[:insert_at] + "\nsetup_agegate(app, templates)\n" + s[insert_at:]

p.write_text(s, encoding="utf-8")
print("OK: app.py patched")
