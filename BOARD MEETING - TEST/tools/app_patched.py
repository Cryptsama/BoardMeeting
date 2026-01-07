# app.py — BoardMeeting (Hardened, Internet-safe, Auto AI Discussion)
# Behavior:
# - NORMAL (/ask): each AI answers the USER directly (NO AI-to-AI reading, NO cross-review)
# - AUTO (/auto_round): AIs read the ROOM TRANSCRIPT and respond to the conversation
# - Detail Mode affects /ask, /auto_round, and /board_decision (if frontend sends detailMode)

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import asyncio, os, time, re
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime
import anthropic

# ─── ENV ──────────────────────────────────────────────────────────────
load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

if not OPENAI_KEY or not GEMINI_KEY:
    raise RuntimeError("Missing API keys")

LOG_FILE = Path(__file__).with_name("BoardMeeting_Notes.txt")

# ─── RATE LIMIT ───────────────────────────────────────────────────────
RATE_LIMIT = {}
MAX_REQ = 20
WINDOW = 60

def check_rate(ip):
    now = time.time()
    hits = RATE_LIMIT.get(ip, [])
    hits = [t for t in hits if now - t < WINDOW]
    if len(hits) >= MAX_REQ:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    hits.append(now)
    RATE_LIMIT[ip] = hits

# ─── AUTO MODE STATE ──────────────────────────────────────────────────
AUTO_STATE = {"running": False, "turn": 0}  # 0=ChatGPT, 1=Gemini, 2=Claude

def next_speaker():
    s = AUTO_STATE["turn"] % 3
    AUTO_STATE["turn"] += 1
    return s

# ─── FASTAPI (DOCS DISABLED) ───────────────────────────────────────────
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="Templates")

# ─── STYLE MODES ──────────────────────────────────────────────────────
STYLE_SHORT = "Reply in 1–2 short sentences. Be direct. No filler."

STYLE_DETAIL = (
    "What you’re asking for is “high-information, low-sentence” output: each sentence is dense, but still simple.\n"
    "Explain like the user is 18: plain words, no jargon unless you define it fast.\n"
    "Max 5–7 sentences total, but each sentence can be long as long as it stays clear.\n"
    "Structure inside those sentences: what it is → why it happens → what to change → exact steps → quick example → one warning.\n"
    "No filler (“sure”, “here’s the thing”, “in conclusion”), and no repeating yourself.\n"
    "If something is unclear, make one assumption and say it, instead of asking 5 questions."
)

def style_for(detail_mode: bool) -> str:
    return STYLE_DETAIL if detail_mode else STYLE_SHORT

def tokens_for(detail_mode: bool, kind: str = "normal") -> int:
    # kind: "normal" or "bootstrap" or "auto" or "board"
    if kind == "bootstrap":
        return 110
    if kind == "auto":
        return 140 if not detail_mode else 260
    if kind == "board":
        return 120 if not detail_mode else 420
    return 140 if not detail_mode else 420

# ─── SYSTEM ROLE LOCKS (IDENTITY ONLY; STYLE IS DYNAMIC) ───────────────
ROLE_SYSTEM = {
    "ChatGPT": (
        "You are ChatGPT (OpenAI). "
        "Never claim to be Gemini or Claude. "
        "If asked your identity, say: 'I’m ChatGPT (OpenAI).'"
    ),
    "Gemini": (
        "You are Gemini (Google). "
        "Never claim to be ChatGPT or Claude. "
        "If asked your identity, say: 'I’m Gemini (Google).'"
    ),
    "Claude": (
        "You are Claude (Anthropic). "
        "Never claim to be ChatGPT or Gemini. "
        "If asked your identity, say: 'I’m Claude (Anthropic).'"
    ),
    "Referee": (
        "You are the referee only. "
        "Never claim to be ChatGPT, Gemini, or Claude. "
        "Your output must start with 'BOARD DECISION:' and contain nothing before it."
    ),
}

def sanitize_identity(text: str, expected: str) -> str:
    t = (text or "").strip()
    if not t:
        return t

    bad = [
        r"\bI\s*am\s*ChatGPT\b", r"\bI'm\s*ChatGPT\b", r"\bChatGPT\s*\(OpenAI\)\b",
        r"\bI\s*am\s*Claude\b",  r"\bI'm\s*Claude\b",  r"\bAnthropic'?s\s*Claude\b",
        r"\bI\s*am\s*Gemini\b",  r"\bI'm\s*Gemini\b",  r"\bGemini\s*\(Google\)\b",
    ]
    for pat in bad:
        t = re.sub(pat, "", t, flags=re.IGNORECASE).strip()

    low = t.lower()
    if expected == "Gemini" and ("chatgpt" in low or "claude" in low):
        t = "I’m Gemini (Google). " + t
    elif expected == "Claude" and ("chatgpt" in low or "gemini" in low):
        t = "I’m Claude (Anthropic). " + t
    elif expected == "ChatGPT" and ("gemini" in low or "claude" in low):
        t = "I’m ChatGPT (OpenAI). " + t

    t = re.sub(r"\s{2,}", " ", t).strip()
    return t

# ─── OPENAI ────────────────────────────────────────────────────────────
from openai import OpenAI
oai = OpenAI(api_key=OPENAI_KEY)

def openai_chat(prompt: str, detail_mode: bool, kind: str = "normal") -> str:
    sys = ROLE_SYSTEM["ChatGPT"] + "\n" + style_for(detail_mode)
    r = oai.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": prompt},
        ],
        max_tokens=tokens_for(detail_mode, kind=kind),
        temperature=0.2
    )
    return (r.choices[0].message.content or "").strip()

def openai_referee(prompt: str, detail_mode: bool) -> str:
    # Referee uses its own system role + (optional) detail style rules in the user prompt.
    r = oai.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": ROLE_SYSTEM["Referee"]},
            {"role": "user", "content": prompt},
        ],
        max_tokens=tokens_for(detail_mode, kind="board"),
        temperature=0.2
    )
    return (r.choices[0].message.content or "").strip()

# ─── GEMINI ────────────────────────────────────────────────────────────
import google.generativeai as genai
genai.configure(api_key=GEMINI_KEY)

models = [
    m.name.split("/")[-1]
    for m in genai.list_models()
    if "generateContent" in getattr(m, "supported_generation_methods", [])
]
if not models:
    raise RuntimeError("No Gemini models available")

MODEL_NAME = models[0]
print(f"[Gemini] Using model: {MODEL_NAME}")

def gem_model_for(detail_mode: bool):
    try:
        return genai.GenerativeModel(
            MODEL_NAME,
            system_instruction=(ROLE_SYSTEM["Gemini"] + "\n" + style_for(detail_mode))
        )
    except TypeError:
        return genai.GenerativeModel(MODEL_NAME)

def gem_generate(detail_mode: bool, prompt: str, kind: str = "normal") -> str:
    m = gem_model_for(detail_mode)
    try:
        g = m.generate_content(prompt, generation_config={"max_output_tokens": tokens_for(detail_mode, kind=kind)})
    except TypeError:
        # Older versions may not accept generation_config as dict
        g = m.generate_content(prompt)
    return sanitize_identity((getattr(g, "text", "") or "").strip(), "Gemini")

# ─── CLAUDE ────────────────────────────────────────────────────────────
async def call_claude_async(user_prompt: str, detail_mode: bool, kind: str = "normal") -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return "Claude disabled."
    client = anthropic.Anthropic(api_key=key)

    system_text = ROLE_SYSTEM["Claude"] + "\n" + style_for(detail_mode)
    max_toks = tokens_for(detail_mode, kind=kind)

    def run():
        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-3-haiku-20240307"),
            max_tokens=max_toks,
            system=system_text,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return (msg.content[0].text or "").strip()

    return sanitize_identity(await asyncio.to_thread(run), "Claude")

# ─── HISTORY HELPERS ──────────────────────────────────────────────────
def tail_history(history, n=40):
    return history[-n:] if history else []

def format_room(history):
    out = []
    for h in history or []:
        who = (h.get("speaker", "user") or "user").lower()
        label = "User"
        if who == "openai": label = "ChatGPT"
        elif who == "gemini": label = "Gemini"
        elif who == "claude": label = "Claude"
        elif who == "board": label = "Board"
        txt = (h.get("text", "") or "").strip()
        if txt:
            out.append(f"[{label}]: {txt}")
    return "\n".join(out)

def only_user_history(history):
    # NORMAL mode: keep only the user's messages as context (no other AIs)
    out = []
    for h in history or []:
        who = (h.get("speaker", "user") or "user").lower()
        if who == "user":
            txt = (h.get("text", "") or "").strip()
            if txt:
                out.append({"speaker": "user", "text": txt})
    return out

def format_user_only(history):
    lines = []
    for h in history or []:
        txt = (h.get("text", "") or "").strip()
        if txt:
            lines.append(f"[User]: {txt}")
    return "\n".join(lines)

ROOM_ROSTER = """BoardMeeting Room Participants:
- ChatGPT (OpenAI)
- Gemini (Google)
- Claude (Anthropic)

Rules:
- Read the ROOM TRANSCRIPT below.
"""

def build_room_prompt(history, user_prompt=None, extra=None):
    room = format_room(history)
    parts = [
        ROOM_ROSTER,
        "ROOM TRANSCRIPT:",
        room if room else "[no messages yet]",
    ]
    if user_prompt:
        parts += ["", f"USER MESSAGE:\n{user_prompt}"]
    if extra:
        parts += ["", extra]
    return "\n".join(parts).strip()

def build_direct_prompt(user_only_hist, user_prompt):
    # NORMAL mode: direct-to-user only (no AI transcript)
    ctx = format_user_only(user_only_hist)
    return (
        "TASK:\nAnswer the user's message directly.\n"
        "Do NOT mention other AIs.\n\n"
        "USER CONTEXT (user messages only):\n"
        f"{ctx if ctx else '[no prior user messages]'}\n\n"
        "USER MESSAGE:\n"
        f"{user_prompt}\n"
    ).strip()

# ─── ROUTES ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/health")
def health():
    return {"status": "ok"}

# ─── BOOTSTRAP ─────────────────────────────────────────────────────────
@app.get("/bootstrap")
async def bootstrap():
    boot_user = "Boot: Introduce yourself in 1 sentence and explicitly acknowledge the other two AIs by name. No questions."
    boot_prompt = build_room_prompt(history=[], user_prompt=boot_user)

    try:
        oai_text = openai_chat(boot_prompt, detail_mode=False, kind="bootstrap")
    except Exception as e:
        oai_text = f"OpenAI error: {e}"

    try:
        gem_text = gem_generate(False, boot_prompt, kind="bootstrap")
    except Exception as e:
        gem_text = f"Gemini error: {e}"

    try:
        claude_text = await call_claude_async(boot_prompt, detail_mode=False, kind="bootstrap")
    except Exception as e:
        claude_text = f"Claude error: {e}"

    return {"messages": [
        {"speaker": "openai", "text": oai_text},
        {"speaker": "gemini", "text": gem_text},
        {"speaker": "claude", "text": claude_text},
    ]}

# ─── USER ASK (DIRECT-TO-USER ONLY) ────────────────────────────────────
@app.post("/ask")
async def ask(request: Request):
    ip = request.client.host
    check_rate(ip)

    data = await request.json()
    user_prompt = (data.get("prompt") or "").strip()
    history = tail_history(data.get("history") or [], n=60)  # UI can send everything
    detail_mode = bool(data.get("detailMode", False))

    if not user_prompt:
        return {"openai": "Empty", "gemini": "Empty", "claude": "Empty"}

    # Filter to user-only context so AIs do NOT read each other in NORMAL mode
    user_only = only_user_history(history)
    direct_prompt = build_direct_prompt(user_only, user_prompt)

    # Run all AIs independently (NO peer review)
    try:
        oai_text = openai_chat(direct_prompt, detail_mode=detail_mode, kind="normal")
    except Exception as e:
        oai_text = f"OpenAI error: {e}"

    try:
        gem_text = gem_generate(detail_mode, direct_prompt, kind="normal")
    except Exception as e:
        gem_text = f"Gemini error: {e}"

    try:
        claude_text = await call_claude_async(direct_prompt, detail_mode=detail_mode, kind="normal")
    except Exception as e:
        claude_text = f"Claude error: {e}"

    # Log
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now()}]\n")
            f.write(f"MODE: NORMAL_DIRECT\n")
            f.write(f"DETAIL_MODE: {detail_mode}\n")
            f.write(f"USER: {user_prompt}\n")
            f.write(f"ChatGPT: {oai_text}\n")
            f.write(f"Gemini: {gem_text}\n")
            f.write(f"Claude: {claude_text}\n")
    except:
        pass

    return {"openai": oai_text, "gemini": gem_text, "claude": claude_text}

# ─── AUTO ROUND (READS FULL ROOM) ─────────────────────────────────────
@app.post("/auto_round")
async def auto_round(req: Request):
    data = await req.json()
    history = tail_history(data.get("history") or [], n=80)
    detail_mode = bool(data.get("detailMode", False))

    AUTO_STATE["running"] = True
    speaker = next_speaker()

    auto_prompt = build_room_prompt(
        history,
        user_prompt=None,
        extra="AUTO MODE: Read the ROOM TRANSCRIPT and respond to what others said. Stay on-topic."
    )

    if speaker == 0:
        try:
            text = openai_chat(auto_prompt, detail_mode=detail_mode, kind="auto")
        except Exception as e:
            text = f"OpenAI error: {e}"
        return {"speaker": "openai", "text": text}

    if speaker == 1:
        try:
            text = gem_generate(detail_mode, auto_prompt, kind="auto")
        except Exception as e:
            text = f"Gemini error: {e}"
        return {"speaker": "gemini", "text": text}

    try:
        text = await call_claude_async(auto_prompt, detail_mode=detail_mode, kind="auto")
    except Exception as e:
        text = f"Claude error: {e}"
    return {"speaker": "claude", "text": text}

# ─── AUTO STOP ─────────────────────────────────────────────────────────
@app.post("/auto_stop")
def auto_stop():
    AUTO_STATE["running"] = False
    AUTO_STATE["turn"] = 0
    return {"status": "stopped"}

# ─── BOARD DECISION (DETAIL MODE AWARE) ────────────────────────────────
@app.post("/board_decision")
async def board_decision(request: Request):
    ip = request.client.host
    check_rate(ip)

    data = await request.json()
    history = tail_history(data.get("history") or [], n=120)
    detail_mode = bool(data.get("detailMode", False))
    room = format_room(history)

    if not detail_mode:
        rules = (
            "Output exactly ONE short sentence.\n"
            "No extra text before/after.\n"
            "If not enough info: BOARD DECISION: Need more context."
        )
    else:
        rules = (
            "Use the DETAIL rules (5–7 dense, simple sentences) to merge the best points and resolve conflicts.\n"
            "Still start with BOARD DECISION: and include nothing before it.\n"
            "If not enough info: BOARD DECISION: Need more context."
        )

    referee_prompt = f"""
TASK:
Read the ROOM TRANSCRIPT and output:
BOARD DECISION: <your decision>

ROOM TRANSCRIPT:
{room if room else "[no messages yet]"}

RULES:
{rules}
""".strip()

    try:
        text = openai_referee(referee_prompt, detail_mode=detail_mode)
    except Exception as e:
        text = f"BOARD DECISION: OpenAI error: {e}"

    if text and not text.lower().startswith("board decision:"):
        text = "BOARD DECISION: " + text.strip()

    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now()}]\n")
            f.write(f"MODE: BOARD_DECISION\n")
            f.write(f"DETAIL_MODE: {detail_mode}\n")
            f.write(f"{text}\n")
    except:
        pass

    return {"board": text}


# ─────────────────────────────────────────────────────────────────────────────
# COUNCIL MODE (LLM-Council-style): saved conversations + SSE + 3-stage ranking
# ─────────────────────────────────────────────────────────────────────────────
import json, uuid
from fastapi.responses import StreamingResponse

COUNCIL_DATA_DIR = Path(__file__).with_name("data").joinpath("conversations")

def _ensure_council_dir():
    COUNCIL_DATA_DIR.mkdir(parents=True, exist_ok=True)

def _convo_path(conversation_id: str) -> Path:
    return COUNCIL_DATA_DIR / f"{conversation_id}.json"

def create_conversation(conversation_id: str) -> dict:
    _ensure_council_dir()
    convo = {
        "id": conversation_id,
        "created_at": datetime.utcnow().isoformat(),
        "title": "New Conversation",
        "messages": []
    }
    _convo_path(conversation_id).write_text(json.dumps(convo, indent=2), encoding="utf-8")
    return convo

def get_conversation(conversation_id: str):
    p = _convo_path(conversation_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))

def save_conversation(convo: dict):
    _ensure_council_dir()
    _convo_path(convo["id"]).write_text(json.dumps(convo, indent=2), encoding="utf-8")

def list_conversations() -> list[dict]:
    _ensure_council_dir()
    items = []
    for p in COUNCIL_DATA_DIR.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        items.append({
            "id": data["id"],
            "created_at": data["created_at"],
            "title": data.get("title", "New Conversation"),
            "message_count": len(data.get("messages", [])),
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items

def add_user_message(conversation_id: str, content: str):
    convo = get_conversation(conversation_id)
    if convo is None:
        raise ValueError("Conversation not found")
    convo["messages"].append({"role": "user", "content": content})
    save_conversation(convo)

def add_assistant_message(conversation_id: str, stage1: list, stage2: list, stage3: dict):
    convo = get_conversation(conversation_id)
    if convo is None:
        raise ValueError("Conversation not found")
    convo["messages"].append({"role": "assistant", "stage1": stage1, "stage2": stage2, "stage3": stage3})
    save_conversation(convo)

def update_conversation_title(conversation_id: str, title: str):
    convo = get_conversation(conversation_id)
    if convo is None:
        raise ValueError("Conversation not found")
    convo["title"] = title
    save_conversation(convo)

def parse_ranking_from_text(ranking_text: str) -> list[str]:
    import re
    if "FINAL RANKING:" in ranking_text:
        parts = ranking_text.split("FINAL RANKING:")
        if len(parts) >= 2:
            section = parts[1]
            numbered = re.findall(r"\d+\.\s*Response [A-Z]", section)
            if numbered:
                return [re.search(r"Response [A-Z]", m).group() for m in numbered]
            return re.findall(r"Response [A-Z]", section)
    return re.findall(r"Response [A-Z]", ranking_text)

def calculate_aggregate_rankings(stage2_results: list[dict], label_to_model: dict[str, str]) -> list[dict]:
    from collections import defaultdict
    model_positions = defaultdict(list)
    for r in stage2_results:
        parsed = r.get("parsed_ranking") or parse_ranking_from_text(r.get("ranking", ""))
        for position, label in enumerate(parsed, start=1):
            if label in label_to_model:
                model_positions[label_to_model[label]].append(position)

    agg = []
    for model, positions in model_positions.items():
        if positions:
            avg = sum(positions) / len(positions)
            agg.append({"model": model, "average_rank": round(avg, 2), "rankings_count": len(positions)})
    agg.sort(key=lambda x: x["average_rank"])
    return agg

async def stage1_collect_responses(user_query: str, detail_mode: bool=False) -> list[dict]:
    tasks = [
        asyncio.to_thread(openai_chat, user_query, detail_mode, "council_stage1"),
        asyncio.to_thread(gem_generate, detail_mode, user_query, "council_stage1"),
    ]
    if os.getenv("ANTHROPIC_API_KEY"):
        tasks.append(asyncio.to_thread(claude_chat, user_query, detail_mode, "council_stage1"))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = []
    names = ["OPENAI", "GEMINI", "CLAUDE"]
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            continue
        if isinstance(res, str) and res.strip():
            out.append({"model": name, "response": res})
    return out

async def stage2_collect_rankings(user_query: str, stage1_results: list[dict], detail_mode: bool=False):
    labels = [chr(65+i) for i in range(len(stage1_results))]  # A,B,C...
    label_to_model = {f"Response {lab}": r["model"] for lab, r in zip(labels, stage1_results)}
    responses_text = "\n\n".join([f"Response {lab}:\n{r['response']}" for lab, r in zip(labels, stage1_results)])

    ranking_prompt = f"""You are evaluating different responses to the following question:

Question: {user_query}

Here are the responses from different models (anonymized):

{responses_text}

Your task:
1. First, evaluate each response individually. For each response, explain what it does well and what it does poorly.
2. Then, at the very end of your response, provide a final ranking.

IMPORTANT: Your final ranking MUST be formatted EXACTLY as follows:
FINAL RANKING:
1. Response A
2. Response B

Now provide your evaluation and ranking:"""

    tasks = [
        asyncio.to_thread(openai_chat, ranking_prompt, detail_mode, "council_stage2"),
        asyncio.to_thread(gem_generate, detail_mode, ranking_prompt, "council_stage2"),
    ]
    if os.getenv("ANTHROPIC_API_KEY"):
        tasks.append(asyncio.to_thread(claude_chat, ranking_prompt, detail_mode, "council_stage2"))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    stage2 = []
    names = ["OPENAI", "GEMINI", "CLAUDE"]
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            continue
        if isinstance(res, str) and res.strip():
            stage2.append({"model": name, "ranking": res, "parsed_ranking": parse_ranking_from_text(res)})

    return stage2, label_to_model

async def stage3_synthesize_final(user_query: str, stage1_results: list[dict], stage2_results: list[dict], detail_mode: bool=False) -> dict:
    stage1_text = "\n\n".join([f"Model: {r['model']}\nResponse: {r['response']}" for r in stage1_results])
    stage2_text = "\n\n".join([f"Model: {r['model']}\nRanking: {r['ranking']}" for r in stage2_results])

    chairman_prompt = f"""You are the Chairman of an LLM Council.

Original Question: {user_query}

STAGE 1 - Individual Responses:
{stage1_text}

STAGE 2 - Peer Rankings:
{stage2_text}

Synthesize ONE final accurate answer. Final answer only (no meta)."""

    final = await asyncio.to_thread(gem_generate, detail_mode, chairman_prompt, "council_stage3")
    return {"model": "GEMINI", "response": final}

async def generate_conversation_title(user_query: str) -> str:
    prompt = f"""Generate a very short title (3-5 words max). No quotes/punctuation.

Question: {user_query}

Title:"""
    title = await asyncio.to_thread(gem_generate, False, prompt, "title")
    title = (title or "New Conversation").strip().strip('"').strip("'")
    return title[:50] if len(title) > 50 else title

async def run_full_council(user_query: str, detail_mode: bool=False):
    stage1 = await stage1_collect_responses(user_query, detail_mode)
    if not stage1:
        return [], [], {"model":"error","response":"All models failed to respond."}, {}
    stage2, label_to_model = await stage2_collect_rankings(user_query, stage1, detail_mode)
    aggregate = calculate_aggregate_rankings(stage2, label_to_model)
    stage3 = await stage3_synthesize_final(user_query, stage1, stage2, detail_mode)
    meta = {"label_to_model": label_to_model, "aggregate_rankings": aggregate}
    return stage1, stage2, stage3, meta

# ----- Conversation API (LLM Council compatible) -----

@app.get("/api/conversations")
async def api_list_conversations():
    return list_conversations()

@app.post("/api/conversations")
async def api_create_conversation():
    conversation_id = str(uuid.uuid4())
    return create_conversation(conversation_id)

@app.get("/api/conversations/{conversation_id}")
async def api_get_conversation(conversation_id: str):
    convo = get_conversation(conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return convo

@app.post("/api/conversations/{conversation_id}/message")
async def api_send_message(conversation_id: str, request: Request):
    convo = get_conversation(conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Missing content")

    is_first = len(convo.get("messages", [])) == 0
    add_user_message(conversation_id, content)

    if is_first:
        title = await generate_conversation_title(content)
        update_conversation_title(conversation_id, title)

    stage1, stage2, stage3, meta = await run_full_council(content, detail_mode=False)
    add_assistant_message(conversation_id, stage1, stage2, stage3)

    return {"stage1": stage1, "stage2": stage2, "stage3": stage3, "metadata": meta}

@app.post("/api/conversations/{conversation_id}/message/stream")
async def api_send_message_stream(conversation_id: str, request: Request):
    convo = get_conversation(conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    try:
        body = await _safe_json(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Missing content")

    is_first = len(convo.get("messages", [])) == 0

    async def event_generator():
        try:
            add_user_message(conversation_id, content)

            title_task = None
            if is_first:
                title_task = asyncio.create_task(generate_conversation_title(content))

            yield f"data: {json.dumps({'type':'stage1_start'})}\n\n"
            stage1 = await stage1_collect_responses(content, detail_mode=False)
            yield f"data: {json.dumps({'type':'stage1_complete','data':stage1})}\n\n"

            yield f"data: {json.dumps({'type':'stage2_start'})}\n\n"
            stage2, label_to_model = await stage2_collect_rankings(content, stage1, detail_mode=False)
            aggregate = calculate_aggregate_rankings(stage2, label_to_model)
            yield f"data: {json.dumps({'type':'stage2_complete','data':stage2,'metadata':{'label_to_model':label_to_model,'aggregate_rankings':aggregate}})}\n\n"

            yield f"data: {json.dumps({'type':'stage3_start'})}\n\n"
            stage3 = await stage3_synthesize_final(content, stage1, stage2, detail_mode=False)
            yield f"data: {json.dumps({'type':'stage3_complete','data':stage3})}\n\n"

            if title_task:
                title = await title_task
                update_conversation_title(conversation_id, title)
                yield f"data: {json.dumps({'type':'title_complete','data':{'title':title}})}\n\n"

            add_assistant_message(conversation_id, stage1, stage2, stage3)
            yield f"data: {json.dumps({'type':'complete'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type':'error','message':str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control":"no-cache","Connection":"keep-alive"}
    )

# ─── LOCAL RUN ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000)

