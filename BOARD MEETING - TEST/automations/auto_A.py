# auto_A.py — Patched BoardMeeting (minimal changes: history trimmed + anti-repeat)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime

# ───────────────────────────────────────────────────────────────
# Load environment variables
# ───────────────────────────────────────────────────────────────
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

if not OPENAI_KEY:
    raise ValueError("Missing OPENAI_API_KEY in .env")
if not GEMINI_KEY:
    raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in .env")

# ───────────────────────────────────────────────────────────────
# DAILY LOG FOLDER
# ───────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ───────────────────────────────────────────────────────────────
# OpenAI client
# ───────────────────────────────────────────────────────────────
from openai import OpenAI
oai = OpenAI(api_key=OPENAI_KEY)

# ───────────────────────────────────────────────────────────────
# Gemini client (Paid chooser)
# ───────────────────────────────────────────────────────────────
import google.generativeai as genai
genai.configure(api_key=GEMINI_KEY)

def _avail_models():
    out = []
    for m in genai.list_models():
        if "generateContent" in getattr(m, "supported_generation_methods", []):
            out.append(m.name.split("/")[-1])
    return out

AVAILABLE = _avail_models()

def _pick_model(prefer=None):
    names = AVAILABLE
    if prefer:
        for n in names:
            if n == prefer or prefer in n:
                return n

    ordering = [
        ("2.5", "flash", "latest"),
        ("2.5", "pro", "latest"),
        ("1.5", "flash", "latest"),
        ("1.5", "pro", "latest"),
    ]

    for req in ordering:
        for n in names:
            if all(x in n for x in req) and "image" not in n:
                return n

    return names[0] if names else None

CHOICE = _pick_model(os.getenv("GEMINI_MODEL", "").strip())
if not CHOICE:
    raise RuntimeError(f"No compatible Gemini model found. Available={AVAILABLE}")

print(f"[Gemini] Using model: {CHOICE}")
gem_model = genai.GenerativeModel(CHOICE)

# ───────────────────────────────────────────────────────────────
# FastAPI setup
# ───────────────────────────────────────────────────────────────
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="Templates")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
def health():
    return {"gemini_model": CHOICE, "available": AVAILABLE[:10]}

# ───────────────────────────────────────────────────────────────
# Helpers: history + anti-repeat
# ───────────────────────────────────────────────────────────────
def trim_history(history, max_items=3):
    if not history:
        return []
    return history[-max_items:]

def format_room_history(history):
    lines = []
    for item in history or []:
        who = (item.get("speaker") or "").lower()
        label = "User"
        if who == "openai":
            label = "ChatGPT"
        elif who == "gemini":
            label = "Gemini"
        txt = (item.get("text") or "").replace("\n", " ").strip()
        if txt:
            lines.append(f"[{label}]: {txt}")
    return "\n".join(lines)

def apply_simple_anti_repeat(text: str, history):
    if not history:
        return text
    last_text = (history[-1].get("text") or "").strip()
    if not last_text:
        return text
    if text.strip() == last_text:
        return "Loop detected — stopping repetition."
    return text

# ───────────────────────────────────────────────────────────────
# /ask — Dual AI + Truth Mode + Daily Logs (patched)
# ───────────────────────────────────────────────────────────────
@app.post("/ask")
async def ask(request: Request):
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()
    history = data.get("history") or []

    if not prompt:
        return JSONResponse({"openai": "Empty prompt", "gemini": "Empty prompt"})

    # PATCH: use only last 3 history items to avoid infinite summary loops
    recent_history = trim_history(history, max_items=3)
    room_log = format_room_history(recent_history)

    truth_block = """
TRUTH MODE:
- Do NOT pretend to browse the internet.
- Do NOT pretend to run code.
- If unsure, say “I’m not sure.”
- Do NOT summarize the entire conversation; answer the user directly.
"""

    # ChatGPT prompt
    oai_prompt = f"""
You are ChatGPT in a room with Gemini.
Room:
{room_log or "[none]"}

User: "{prompt}"

{truth_block}

Respond ONLY as ChatGPT.
Max 2 short sentences.
"""

    # Gemini prompt
    gem_prompt = f"""
You are Gemini in a room with ChatGPT.
Room:
{room_log or "[none]"}

User: "{prompt}"

{truth_block}

Respond ONLY as Gemini.
Max 2 short sentences.
"""

    # ChatGPT call
    try:
        r = oai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": oai_prompt}],
            temperature=0.3,
            max_tokens=60,
        )
        oai_text = (r.choices[0].message.content or "").strip()
    except Exception as e:
        oai_text = f"OpenAI error: {e}"

    # Gemini call
    try:
        r2 = gem_model.generate_content(gem_prompt)
        gem_text = (getattr(r2, "text", "") or "").strip()
    except Exception as e:
        gem_text = f"Gemini error: {e}"

    # PATCH: simple anti-repeat vs last history text
    oai_text = apply_simple_anti_repeat(oai_text, history)
    gem_text = apply_simple_anti_repeat(gem_text, history)

    # ───────────────────────────────────────────────
    # DAILY ROTATING LOG SYSTEM
    # ───────────────────────────────────────────────
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_log = LOG_DIR / f"BoardMeeting_Notes_{today}.txt"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with daily_log.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] /ask\n")
            f.write(f"USER: {prompt}\n")
            f.write(f"ChatGPT: {oai_text}\n")
            f.write(f"Gemini: {gem_text}\n\n")
    except Exception as e:
        print("Logging error:", e)

    return JSONResponse({"openai": oai_text, "gemini": gem_text})

# ───────────────────────────────────────────────────────────────
# /auto_round — also writes to daily logs (patched)
# ───────────────────────────────────────────────────────────────
@app.post("/auto_round")
async def auto_round(request: Request):
    data = await request.json()
    topic = (data.get("topic") or "").strip()
    history = data.get("history") or []

    # PATCH: limit history again
    recent_history = trim_history(history, max_items=3)
    room_log = format_room_history(recent_history)

    auto_inst = f"""
Topic: {topic or "the current topic"}
Follow TRUTH MODE.
Respond in 2 short sentences.
Do NOT summarize the entire past; speak directly to this topic.
"""

    # ChatGPT
    try:
        r = oai.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": f"{room_log}\n\n{auto_inst}"}],
            temperature=0.3,
            max_tokens=80,
        )
        oai_text = (r.choices[0].message.content or "").strip()
    except Exception as e:
        oai_text = f"OpenAI error: {e}"

    # Gemini
    try:
        r2 = gem_model.generate_content(f"{room_log}\n\n{auto_inst}")
        gem_text = (getattr(r2, "text", "") or "").strip()
    except Exception as e:
        gem_text = f"Gemini error: {e}"

    # PATCH: simple anti-repeat vs last history text
    oai_text = apply_simple_anti_repeat(oai_text, history)
    gem_text = apply_simple_anti_repeat(gem_text, history)

    # Daily log
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_log = LOG_DIR / f"BoardMeeting_Notes_{today}.txt"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with daily_log.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] /auto_round\n")
            f.write(f"TOPIC: {topic}\n")
            f.write(f"ChatGPT: {oai_text}\n")
            f.write(f"Gemini: {gem_text}\n\n")
    except:
        pass

    return JSONResponse({"openai": oai_text, "gemini": gem_text})

# ───────────────────────────────────────────────────────────────
# /web — DuckDuckGo search (unchanged)
# ───────────────────────────────────────────────────────────────
@app.post("/web")
async def web(request: Request):
    data = await request.json()
    query = (data.get("query") or "").strip()
    if not query:
        return JSONResponse({"results": ["Empty query"]})

    try:
        from websearch import ddg_search
        return JSONResponse({"results": ddg_search(query, n=5)})
    except Exception as e:
        return JSONResponse({"results": [f"Search error: {e}"]})

# ───────────────────────────────────────────────────────────────
# Local run
# ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("auto_A:app", host="0.0.0.0", port=8000, reload=True)
