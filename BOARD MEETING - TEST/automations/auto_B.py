# auto_B.py — BoardMeeting (OpenAI + Gemini + Claude)
# - 45s timeout per AI
# - History trimmed (last 3 turns) to reduce "the conversation began..." loops
# - Anti-repeat: detects when answers are basically the same as last turn
# - Daily log files in ./logs
# - Multi-AI awareness: each model sees [ChatGPT]/[Gemini]/[Claude] lines in Room:

import asyncio
import functools
import difflib
import os
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dotenv import load_dotenv

# ───────────────────────────────────────────────
# Load environment variables
# ───────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-3-haiku-20240307"

print(f"[DEBUG] .env path: {ENV_PATH}")
print(f"[DEBUG] OPENAI_KEY set: {bool(OPENAI_KEY)}")
print(f"[DEBUG] GEMINI_KEY set: {bool(GEMINI_KEY)}")
print(f"[DEBUG] ANTHROPIC_KEY set: {bool(ANTHROPIC_KEY)}")
print(f"[DEBUG] CLAUDE_MODEL: {CLAUDE_MODEL}")

if not OPENAI_KEY:
    raise ValueError("Missing OPENAI_API_KEY in .env")
if not GEMINI_KEY:
    raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY in .env")

ENABLE_CLAUDE = bool(ANTHROPIC_KEY)

# ───────────────────────────────────────────────
# DAILY LOG FOLDER
# ───────────────────────────────────────────────
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ───────────────────────────────────────────────
# OpenAI client
# ───────────────────────────────────────────────
from openai import OpenAI
oai = OpenAI(api_key=OPENAI_KEY)

# ───────────────────────────────────────────────
# Gemini client
# ───────────────────────────────────────────────
import google.generativeai as genai
genai.configure(api_key=GEMINI_KEY)


def _avail_models():
    out = []
    for m in genai.list_models():
        if "generateContent" in getattr(m, "supported_generation_methods", []):
            out.append(m.name.split("/")[-1])
    return out


AVAILABLE_GEM = _avail_models()


def _pick_model(prefer=None):
    names = AVAILABLE_GEM
    if not names:
        return None

    if prefer:
        prefer = prefer.strip()
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

    return names[0]


GEM_CHOICE = _pick_model(os.getenv("GEMINI_MODEL", ""))
if not GEM_CHOICE:
    raise RuntimeError(f"No compatible Gemini model found. Available={AVAILABLE_GEM}")

print(f"[Gemini] Using model: {GEM_CHOICE}")
gem_model = genai.GenerativeModel(GEM_CHOICE)

# ───────────────────────────────────────────────
# Claude client (Anthropic)
# ───────────────────────────────────────────────
try:
    if ENABLE_CLAUDE:
        from anthropic import Anthropic
        claude_client = Anthropic(api_key=ANTHROPIC_KEY)
        print("[Claude] Anthropic client configured.")
    else:
        claude_client = None
        print("[Claude] ANTHROPIC_API_KEY not set — Claude disabled.")
except Exception as e:
    claude_client = None
    ENABLE_CLAUDE = False
    print("[Claude] Failed to configure client:", e)

# ───────────────────────────────────────────────
# FastAPI setup
# ───────────────────────────────────────────────
app = FastAPI()

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "Templates"))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {
        "gemini_model": GEM_CHOICE,
        "gemini_available": AVAILABLE_GEM[:10],
        "claude_enabled": ENABLE_CLAUDE,
        "claude_model": CLAUDE_MODEL if ENABLE_CLAUDE else None,
    }

# ───────────────────────────────────────────────
# Utils
# ───────────────────────────────────────────────


async def run_blocking(func, *args, **kwargs):
    """
    Run a blocking function in a thread so we can await it + timeout.
    """
    loop = asyncio.get_event_loop()
    pfunc = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, pfunc)


def similarity(a: str, b: str) -> float:
    """Rough semantic-repeat detector."""
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def trim_history(history, max_items=3):
    """Keep only the last N history items to avoid summary loops."""
    if not history:
        return []
    return history[-max_items:]


def format_room_history(history):
    """
    history is a list of dicts like:
    { "speaker": "user"|"openai"|"gemini"|"claude", "text": "..." }
    """
    lines = []
    for item in history or []:
        who = (item.get("speaker") or "").lower()
        if who == "openai":
            label = "ChatGPT"
        elif who == "gemini":
            label = "Gemini"
        elif who == "claude":
            label = "Claude"
        else:
            label = "User"

        txt = (item.get("text") or "").replace("\n", " ").strip()
        if txt:
            lines.append(f"[{label}]: {txt}")
    return "\n".join(lines)


TRUTH_BLOCK = """
TRUTH MODE:
- You are in a group chat with other AIs and a human user.
- The 'Room:' section shows the shared conversation with tags like [ChatGPT], [Gemini], [Claude], [User].
- Treat those tags as the other participants in the room.
- IMPORTANT: Never speak as another participant. Do NOT write lines starting with “[ChatGPT]:”, “[Gemini]:” or “[Claude]:”. Only answer in your own voice.
- Do NOT pretend to browse the internet or run code.
- If unsure, say “I’m not sure.”
- Do NOT summarize the entire past; answer the CURRENT user topic directly.
"""

# ───────────────────────────────────────────────
# /ask — One round with all AIs
# ───────────────────────────────────────────────


@app.post("/ask")
async def ask(request: Request):
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()
    history = data.get("history") or []

    if not prompt:
        return JSONResponse(
            {"openai": "Empty prompt", "gemini": "Empty prompt", "claude": "Empty prompt"}
        )

    recent_history = trim_history(history, max_items=3)
    room_log = format_room_history(recent_history) or "[none]"

    # ChatGPT prompt
    oai_prompt = f"""
You are ChatGPT in a room with Gemini and Claude.
The Room below is the shared transcript between User, ChatGPT, Gemini, and Claude.
Use it to understand context and what others said, but stay on the user's CURRENT topic.

Room:
{room_log}

User: "{prompt}"

{TRUTH_BLOCK}

Respond ONLY as ChatGPT.
Max 2 short sentences.
""".strip()

    # Gemini prompt
    gem_prompt = f"""
You are Gemini in a room with ChatGPT and Claude.
The Room below is the shared transcript between User, ChatGPT, Gemini, and Claude.
Use it to understand context and what others said, but stay on the user's CURRENT topic.

Room:
{room_log}

User: "{prompt}"

{TRUTH_BLOCK}

Respond ONLY as Gemini.
Max 2 short sentences.
""".strip()

    # Claude prompt
    claude_prompt = f"""
You are Claude in a room with ChatGPT and Gemini.
The Room below is the shared transcript between User, ChatGPT, Gemini, and Claude.
Use it to understand context and what others said, but stay on the user's CURRENT topic.

Room:
{room_log}

User: "{prompt}"

{TRUTH_BLOCK}

Respond ONLY as Claude.
Max 2 short sentences.
""".strip()

    async def call_openai():
        r = await run_blocking(
            oai.chat.completions.create,
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": oai_prompt}],
            temperature=0.3,
            max_tokens=60,
        )
        return (r.choices[0].message.content or "").strip()

    async def call_gemini():
        r2 = await run_blocking(gem_model.generate_content, gem_prompt)
        return (getattr(r2, "text", "") or "").strip()

    async def call_claude():
        if not (ENABLE_CLAUDE and claude_client):
            return "Claude disabled (no API key)."

        try:
            r3 = await run_blocking(
                claude_client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=180,
                temperature=0.3,
                messages=[{"role": "user", "content": claude_prompt}],
            )
            parts = []
            for block in r3.content:
                if getattr(block, "type", None) == "text":
                    parts.append(block.text)
            return (" ".join(parts)).strip() or "[Claude returned empty text]"
        except Exception as e:
            print("Claude error:", e)
            return f"Claude error: {e}"

    try:
        oai_task = asyncio.wait_for(call_openai(), timeout=45)
        gem_task = asyncio.wait_for(call_gemini(), timeout=45)
        if ENABLE_CLAUDE and claude_client:
            claude_task = asyncio.wait_for(call_claude(), timeout=45)
            oai_text, gem_text, claude_text = await asyncio.gather(
                oai_task, gem_task, claude_task
            )
        else:
            oai_text, gem_text = await asyncio.gather(oai_task, gem_task)
            claude_text = "Claude disabled (no API key)."
    except asyncio.TimeoutError:
        return JSONResponse(
            {
                "openai": "Timeout: ChatGPT took too long.",
                "gemini": "Timeout: Gemini took too long.",
                "claude": "Timeout: Claude took too long.",
            },
            status_code=504,
        )

    # Anti-repeat vs last text
    if history:
        last_text = (history[-1].get("text") or "").strip()
        if last_text:
            if similarity(oai_text, last_text) > 0.80:
                oai_text = "Loop detected — stopping repetition."
            if similarity(gem_text, last_text) > 0.80:
                gem_text = "Loop detected — stopping repetition."
            if similarity(claude_text, last_text) > 0.80:
                claude_text = "Loop detected — stopping repetition."

    # Daily log
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_log = LOG_DIR / f"BoardMeeting_Notes_{today}.txt"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with daily_log.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] /ask\n")
            f.write(f"USER: {prompt}\n")
            f.write(f"ChatGPT: {oai_text}\n")
            f.write(f"Gemini: {gem_text}\n")
            f.write(f"Claude: {claude_text}\n\n")
    except Exception as e:
        print("Logging error (/ask):", e)

    return JSONResponse({"openai": oai_text, "gemini": gem_text, "claude": claude_text})

# ───────────────────────────────────────────────
# /auto_round — autonomous follow-up
# ───────────────────────────────────────────────


@app.post("/auto_round")
async def auto_round(request: Request):
    data = await request.json()
    topic = (data.get("topic") or "").strip()
    history = data.get("history") or []

    recent_history = trim_history(history, max_items=3)
    room_log = format_room_history(recent_history) or "[none]"

    auto_inst = f"""
Topic: {topic or "the current topic"}.
Follow TRUTH MODE.
Use the Room transcript to see what others just said.
Stay on THIS topic; do NOT drift back to old meta-discussion.
Respond in 2 short sentences, no long summaries.
""".strip()

    async def call_openai_auto():
        r = await run_blocking(
            oai.chat.completions.create,
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": f"{room_log}\n\n{auto_inst}"}],
            temperature=0.3,
            max_tokens=80,
        )
        return (r.choices[0].message.content or "").strip()

    async def call_gemini_auto():
        r2 = await run_blocking(gem_model.generate_content, f"{room_log}\n\n{auto_inst}")
        return (getattr(r2, "text", "") or "").strip()

    async def call_claude_auto():
        if not (ENABLE_CLAUDE and claude_client):
            return "Claude disabled (no API key)."

        try:
            r3 = await run_blocking(
                claude_client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=180,
                temperature=0.3,
                messages=[
                    {
                        "role": "user",
                        "content": f"{room_log}\n\n{auto_inst}",
                    }
                ],
            )
            parts = []
            for block in r3.content:
                if getattr(block, "type", None) == "text":
                    parts.append(block.text)
            return (" ".join(parts)).strip() or "[Claude returned empty text]"
        except Exception as e:
            print("Claude error (auto_round):", e)
            return f"Claude error: {e}"

    try:
        oai_task = asyncio.wait_for(call_openai_auto(), timeout=45)
        gem_task = asyncio.wait_for(call_gemini_auto(), timeout=45)
        if ENABLE_CLAUDE and claude_client:
            claude_task = asyncio.wait_for(call_claude_auto(), timeout=45)
            oai_text, gem_text, claude_text = await asyncio.gather(
                oai_task, gem_task, claude_task
            )
        else:
            oai_text, gem_text = await asyncio.gather(oai_task, gem_task)
            claude_text = "Claude disabled (no API key)."
    except asyncio.TimeoutError:
        return JSONResponse(
            {
                "openai": "Timeout: ChatGPT took too long in auto_round.",
                "gemini": "Timeout: Gemini took too long in auto_round.",
                "claude": "Timeout: Claude took too long in auto_round.",
            },
            status_code=504,
        )

    if history:
        last_text = (history[-1].get("text") or "").strip()
        if last_text:
            if similarity(oai_text, last_text) > 0.80:
                oai_text = "Auto loop detected — stopping."
            if similarity(gem_text, last_text) > 0.80:
                gem_text = "Auto loop detected — stopping."
            if similarity(claude_text, last_text) > 0.80:
                claude_text = "Auto loop detected — stopping."

    # Daily log
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        daily_log = LOG_DIR / f"BoardMeeting_Notes_{today}.txt"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with daily_log.open("a", encoding="utf-8") as f:
            f.write(f"[{now}] /auto_round\n")
            f.write(f"TOPIC: {topic}\n")
            f.write(f"ChatGPT: {oai_text}\n")
            f.write(f"Gemini: {gem_text}\n")
            f.write(f"Claude: {claude_text}\n\n")
    except Exception as e:
        print("Logging error (/auto_round):", e)

    return JSONResponse({"openai": oai_text, "gemini": gem_text, "claude": claude_text})

# ───────────────────────────────────────────────
# /web — DuckDuckGo search (unchanged)
# ───────────────────────────────────────────────


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

# ───────────────────────────────────────────────
# Local run
# ───────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("auto_B:app", host="0.0.0.0", port=8000, reload=True)
