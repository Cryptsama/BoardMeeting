# app.py — BoardMeeting (Hardened, Internet-safe, Auto AI Discussion + GLOBAL LIVE CHAT)
# Behavior:
# - NORMAL (/ask): each AI answers the USER directly (NO AI-to-AI reading, NO cross-review)
# - AUTO (/auto_round): AIs read the ROOM TRANSCRIPT and respond to the conversation
# - LIVE CHAT (/ws/chat): global chat room for humans (NO API KEYS NEEDED)

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import asyncio, os, time, re, json, base64, uuid, copy
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime
import urllib.request, urllib.parse
from urllib.error import URLError, HTTPError

import anthropic

# ── BoardMeeting add-ons ──
from music_routes import router as music_router


# ─── BM SUPER LOGGER (monthly folders + daily TXT + JSONL + SQLite FTS) ──
BM_LOGGER_OK = False
try:
    from memory_vault import init_db, log_event, recall
    init_db()
    BM_LOGGER_OK = True
    print("[BM] memory_vault: ENABLED")
except Exception as e:
    print(f"[BM] memory_vault: DISABLED ({type(e).__name__}: {e})")

    def log_event(*, session_id: str, role: str, source: str, content: str, meta=None) -> int:
        return -1

    def recall(*, query: str, limit: int = 12, session_id=None):
        return []


def bm_session_id(request: Request, data: dict | None = None) -> str:
    if isinstance(data, dict):
        sid = (data.get("session_id") or "").strip()
        if sid:
            return sid
    ip = getattr(request.client, "host", None) or "unknown"
    return str(ip).strip() or "unknown"


# ─── ENV ───────────────────────────────────────────────────────────────
load_dotenv()

ENV_OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ENV_GEMINI_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
ENV_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-haiku-20240307").strip()

LOG_FILE = Path(__file__).with_name("BoardMeeting_Notes.txt")


# ─── RATE LIMIT ────────────────────────────────────────────────────────
RATE_LIMIT = {}
MAX_REQ = 30
WINDOW = 60

def check_rate(ip):
    now = time.time()
    hits = RATE_LIMIT.get(ip, [])
    hits = [t for t in hits if now - t < WINDOW]
    if len(hits) >= MAX_REQ:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    hits.append(now)
    RATE_LIMIT[ip] = hits


# ─── AUTO MODE STATE ───────────────────────────────────────────────────
AUTO_STATE = {"running": False, "turn": 0}  # 0=ChatGPT, 1=Gemini, 2=Claude

def next_speaker():
    s = int(AUTO_STATE["turn"]) % 3
    AUTO_STATE["turn"] = int(AUTO_STATE["turn"]) + 1
    return s


# ─── FASTAPI (DOCS DISABLED) ───────────────────────────────────────────
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# ✅ mount music endpoints
app.include_router(music_router, prefix="/music", tags=["music"])

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates" if Path("templates").exists() else "Templates")


# ─── KEY HANDLING (per-request keys from frontend headers) ─────────────
def get_keys_from_request(req: Request) -> tuple[str, str, str]:
    # Frontend sends: X-OPENAI-KEY, X-GEMINI-KEY, X-ANTHROPIC-KEY
    o = (req.headers.get("X-OPENAI-KEY") or "").strip() or ENV_OPENAI_KEY
    g = (req.headers.get("X-GEMINI-KEY") or "").strip() or ENV_GEMINI_KEY
    c = (req.headers.get("X-ANTHROPIC-KEY") or "").strip() or ENV_ANTHROPIC_KEY
    return o, g, c


# ─── STYLE MODES ───────────────────────────────────────────────────────
STYLE_SHORT = "Reply in 1–2 short sentences. Be direct. No filler."
STYLE_DETAIL = (
    "Be thorough but structured. Use short paragraphs and bullets when helpful. "
    "Keep it grounded in the prompt. No unnecessary filler."
)

def style_for(detail_mode: bool) -> str:
    return STYLE_DETAIL if detail_mode else STYLE_SHORT

def tokens_for(detail_mode: bool, kind: str = "normal") -> int:
    if kind == "bootstrap":
        return 110
    if kind == "auto":
        return 140 if not detail_mode else 260
    if kind == "board":
        return 120 if not detail_mode else 420
    return 140 if not detail_mode else 420


# ─── SYSTEM ROLE LOCKS ────────────────────────────────────────────────
ROLE_SYSTEM = {
    "ChatGPT": (
        "You are ChatGPT (OpenAI). "
        "Never claim to be Gemini or Claude. "
        "If asked your identity, say: 'I’m ChatGPT (OpenAI).'"),
    "Gemini": (
        "You are Gemini (Google). "
        "Never claim to be ChatGPT or Claude. "
        "If asked your identity, say: 'I’m Gemini (Google).'"),
    "Claude": (
        "You are Claude (Anthropic). "
        "Never claim to be ChatGPT or Gemini. "
        "If asked your identity, say: 'I’m Claude (Anthropic).'"),
    "Referee": (
        "You are the referee only. "
        "Never claim to be ChatGPT, Gemini, or Claude. "
        "Your output must start with 'BOARD DECISION:' and contain nothing before it.")
}

def sanitize_identity(text: str, expected: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    wrong = {
        "ChatGPT": ["I’m Gemini", "I'm Gemini", "I’m Claude", "I'm Claude"],
        "Gemini": ["I’m ChatGPT", "I'm ChatGPT", "I’m Claude", "I'm Claude"],
        "Claude": ["I’m ChatGPT", "I'm ChatGPT", "I’m Gemini", "I'm Gemini"],
    }
    for bad in wrong.get(expected, []):
        t = t.replace(bad, "").strip()
    t = re.sub(r"^As an AI language model[, ]*", "", t, flags=re.I).strip()
    return t


# ─── OPENAI ────────────────────────────────────────────────────────────
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

def openai_chat(api_key: str, prompt: str, detail_mode: bool, kind: str = "normal") -> str:
    if not api_key or OpenAI is None:
        return "OpenAI disabled."
    client = OpenAI(api_key=api_key)
    sys = ROLE_SYSTEM["ChatGPT"] + "\n" + style_for(detail_mode)
    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": prompt},
        ],
        max_tokens=tokens_for(detail_mode, kind=kind),
        temperature=0.2
    )
    return (r.choices[0].message.content or "").strip()

def openai_referee(api_key: str, prompt: str, detail_mode: bool) -> str:
    if not api_key or OpenAI is None:
        return "BOARD DECISION: OpenAI disabled."
    client = OpenAI(api_key=api_key)
    r = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": ROLE_SYSTEM["Referee"]},
            {"role": "user", "content": prompt},
        ],
        max_tokens=tokens_for(detail_mode, kind="board"),
        temperature=0.2
    )
    return (r.choices[0].message.content or "").strip()


# ─── GEMINI ────────────────────────────────────────────────────────────
try:
    import google.generativeai as genai
except Exception:
    genai = None

GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "").strip()

def _gem_pick_model_name(api_key: str) -> str:
    if not api_key or genai is None:
        return ""
    if GEMINI_MODEL:
        return GEMINI_MODEL
    genai.configure(api_key=api_key)
    models = [
        m.name.split("/")[-1]
        for m in genai.list_models()
        if "generateContent" in getattr(m, "supported_generation_methods", [])
    ]
    return models[0] if models else ""

def gem_generate(api_key: str, detail_mode: bool, prompt: str, kind: str = "normal") -> str:
    if not api_key or genai is None:
        return "Gemini disabled."
    genai.configure(api_key=api_key)
    model_name = _gem_pick_model_name(api_key)
    if not model_name:
        return "Gemini disabled."
    try:
        m = genai.GenerativeModel(
            model_name,
            system_instruction=(ROLE_SYSTEM["Gemini"] + "\n" + style_for(detail_mode)),
        )
        g = m.generate_content(
            prompt,
            generation_config={"temperature": 0.2, "max_output_tokens": tokens_for(detail_mode, kind=kind)},
        )
        return sanitize_identity((getattr(g, "text", "") or "").strip(), "Gemini")
    except TypeError:
        m = genai.GenerativeModel(model_name)
        g = m.generate_content(prompt)
        return sanitize_identity((getattr(g, "text", "") or "").strip(), "Gemini")
    except Exception as e:
        return f"Gemini error: {type(e).__name__}: {e}"


# ─── CLAUDE ───────────────────────────────────────────────────────────
async def call_claude_async(api_key: str, user_prompt: str, detail_mode: bool, kind: str = "normal") -> str:
    if not api_key:
        return "Claude disabled."
    client = anthropic.Anthropic(api_key=api_key)
    system_text = ROLE_SYSTEM["Claude"] + "\n" + style_for(detail_mode)
    max_toks = tokens_for(detail_mode, kind=kind)

    def run() -> str:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_toks,
            system=system_text,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return (msg.content[0].text or "").strip()

    try:
        return sanitize_identity(await asyncio.to_thread(run), "Claude")
    except Exception as e:
        return f"Claude error: {type(e).__name__}: {e}"


# ─── HISTORY HELPERS ───────────────────────────────────────────────────
def tail_history(history, n=60):
    return (history or [])[-n:]

def _get_speaker(item):
    return (item.get("speaker") or item.get("role") or "").strip().lower()

def _get_text(item):
    return (item.get("text") or item.get("content") or item.get("message") or "").strip()

def only_user_history(history):
    out = []
    for h in history or []:
        sp = _get_speaker(h)
        if sp in ("user", "you"):
            t = _get_text(h)
            if t:
                out.append({"speaker": "user", "text": t})
    return out

def format_room(history):
    lines = []
    for h in history or []:
        sp = _get_speaker(h)
        t = _get_text(h)
        if not t:
            continue
        if sp in ("user", "you"):
            lines.append(f"USER: {t}")
        elif sp in ("openai", "chatgpt", "gpt"):
            lines.append(f"CHATGPT: {t}")
        elif sp in ("gemini",):
            lines.append(f"GEMINI: {t}")
        elif sp in ("claude",):
            lines.append(f"CLAUDE: {t}")
        else:
            lines.append(f"{sp.upper() if sp else 'MSG'}: {t}")
    return "\n".join(lines).strip()

ROOM_ROSTER = (
    "ROOM ROSTER:\n"
    "- CHATGPT (OpenAI)\n"
    "- GEMINI (Google)\n"
    "- CLAUDE (Anthropic)\n"
)

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
    ctx_lines = []
    for h in user_only_hist or []:
        t = _get_text(h)
        if t:
            ctx_lines.append(f"USER: {t}")
    ctx = "\n".join(ctx_lines[-30:]).strip()
    if ctx:
        return f"USER CONTEXT:\n{ctx}\n\nUSER MESSAGE:\n{user_prompt}".strip()
    return f"USER MESSAGE:\n{user_prompt}".strip()


# ─── COMFYUI TXT2IMG BRIDGE ────────────────────────────────────────────
COMFY_BASE_URL = (os.getenv("COMFY_BASE_URL") or "").strip()
COMFY_API_WORKFLOW_FILE = (os.getenv("COMFY_API_WORKFLOW_FILE") or "workflows/workflow_api.json").strip()
COMFY_POS_NODE_ID = (os.getenv("COMFY_POS_NODE_ID") or "3").strip()
COMFY_NEG_NODE_ID = (os.getenv("COMFY_NEG_NODE_ID") or "4").strip()
COMFY_TIMEOUT_SEC = float(os.getenv("COMFY_TIMEOUT_SEC") or "120")
COMFY_POLL_INTERVAL = float(os.getenv("COMFY_POLL_INTERVAL") or "0.5")

COMFY_LOCK_POS_PREFIX = (os.getenv("COMFY_LOCK_POS_PREFIX") or
    "adult, mature adult, fully grown, legal age, age 25+, 25 years old, not young-looking"
).strip()

COMFY_LOCK_NEG_SUFFIX = (os.getenv("COMFY_LOCK_NEG_SUFFIX") or
    "child, minor, underage, teen, teenager, kid, baby, toddler, young-looking, schoolgirl, schoolboy, loli, shota, petite, childlike, youthful face"
).strip()

_BAD_AGE_WORDS_RE = re.compile(r"\b(underage|minor|child|kid|teen|teenager|schoolgirl|schoolboy|baby|toddler|loli|shota)\b", re.I)
_BAD_AGE_NUM_RE = re.compile(r"\bage\s*(?:[1-9]|1\d|2[0-4])\b", re.I)
_BAD_AGE_YO_RE  = re.compile(r"\b(?:[1-9]|1\d|2[0-4])\s*(?:yo|y/o|years?\s*old)\b", re.I)

_COMFY_WORKFLOW_CACHE = None

def _comfy_required_url() -> str:
    if not COMFY_BASE_URL:
        raise HTTPException(status_code=500, detail="COMFY_BASE_URL is missing in .env")
    return COMFY_BASE_URL.rstrip("/")

def _comfy_workflow_path() -> Path:
    p = Path(COMFY_API_WORKFLOW_FILE)
    if p.is_absolute():
        return p
    return (Path(__file__).parent / p).resolve()

def _comfy_load_workflow() -> dict:
    global _COMFY_WORKFLOW_CACHE
    if _COMFY_WORKFLOW_CACHE is None:
        p = _comfy_workflow_path()
        if not p.exists():
            raise HTTPException(status_code=500, detail=f"Missing workflow file: {p}")
        _COMFY_WORKFLOW_CACHE = json.loads(p.read_text(encoding="utf-8"))
    return _COMFY_WORKFLOW_CACHE

def _comfy_http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"ComfyUI HTTPError {e.code}: {body or e.reason}")
    except URLError as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI URLError: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI error: {type(e).__name__}: {e}")

def _comfy_http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI view error: {type(e).__name__}: {e}")

def _hardlock_age_prompts(user_positive: str, user_negative: str) -> tuple[str, str]:
    up = (user_positive or "").strip()
    un = (user_negative or "").strip()

    combo = f"{up}\n{un}"
    if _BAD_AGE_WORDS_RE.search(combo) or _BAD_AGE_NUM_RE.search(combo) or _BAD_AGE_YO_RE.search(combo):
        raise HTTPException(status_code=400, detail="Blocked: adult-only. Remove any minor/under-25 language.")

    pos = (COMFY_LOCK_POS_PREFIX + (", " + up if up else "")).strip()

    neg_parts = []
    if un:
        neg_parts.append(un)
    if COMFY_LOCK_NEG_SUFFIX:
        neg_parts.append(COMFY_LOCK_NEG_SUFFIX)
    age_nums = [f"age {i}" for i in range(1, 25)]
    neg_parts.append(", ".join(age_nums))

    neg = ", ".join([p for p in neg_parts if p]).strip()
    return pos, neg

def _comfy_apply_prompts(workflow_graph: dict, positive: str, negative: str) -> dict:
    g = copy.deepcopy(workflow_graph)
    pos_id = str(COMFY_POS_NODE_ID)
    neg_id = str(COMFY_NEG_NODE_ID)
    if pos_id not in g or neg_id not in g:
        raise HTTPException(status_code=500, detail=f"Prompt node ids not found in workflow. pos={pos_id} neg={neg_id}")
    g[pos_id].setdefault("inputs", {})
    g[neg_id].setdefault("inputs", {})
    g[pos_id]["inputs"]["text"] = positive
    g[neg_id]["inputs"]["text"] = negative or ""
    return g

def _comfy_queue_prompt(graph: dict) -> str:
    base = _comfy_required_url()
    payload = {"prompt": graph, "client_id": str(uuid.uuid4())}
    r = _comfy_http_json("POST", f"{base}/prompt", payload)
    pid = r.get("prompt_id")
    if not pid:
        raise HTTPException(status_code=502, detail=f"ComfyUI /prompt returned no prompt_id: {r}")
    return str(pid)

def _comfy_wait_history(prompt_id: str) -> dict:
    base = _comfy_required_url()
    deadline = time.time() + COMFY_TIMEOUT_SEC
    last = None
    while time.time() < deadline:
        last = _comfy_http_json("GET", f"{base}/history/{urllib.parse.quote(prompt_id)}")
        if isinstance(last, dict) and prompt_id in last:
            item = last[prompt_id]
            if isinstance(item, dict) and item.get("outputs"):
                return item
        time.sleep(COMFY_POLL_INTERVAL)
    raise HTTPException(status_code=504, detail=f"Timed out waiting for ComfyUI. Last={last}")

def _comfy_first_image_meta(hist_item: dict) -> dict:
    outs = hist_item.get("outputs") or {}
    for _, out in outs.items():
        if not isinstance(out, dict):
            continue
        imgs = out.get("images") or []
        if imgs and isinstance(imgs, list) and isinstance(imgs[0], dict):
            return imgs[0]
    raise HTTPException(status_code=502, detail="ComfyUI finished but returned no images in history.")

def _comfy_fetch_image_bytes(img_meta: dict) -> bytes:
    base = _comfy_required_url()
    qs = urllib.parse.urlencode({
        "filename": img_meta.get("filename", ""),
        "subfolder": img_meta.get("subfolder", ""),
        "type": img_meta.get("type", "output"),
    })
    return _comfy_http_bytes(f"{base}/view?{qs}")

@app.post("/img/txt2img")
async def img_txt2img(request: Request):
    data = await request.json()
    user_positive = (data.get("prompt") or "").strip()
    user_negative = (data.get("negative") or "").strip()
    if not user_positive:
        raise HTTPException(status_code=400, detail="Missing prompt")

    locked_positive, locked_negative = _hardlock_age_prompts(user_positive, user_negative)

    def run():
        wf = _comfy_load_workflow()
        graph = _comfy_apply_prompts(wf, locked_positive, locked_negative)
        pid = _comfy_queue_prompt(graph)
        hist = _comfy_wait_history(pid)
        img_meta = _comfy_first_image_meta(hist)
        img_bytes = _comfy_fetch_image_bytes(img_meta)
        return pid, img_bytes

    pid, img_bytes = await asyncio.to_thread(run)
    return {"prompt_id": pid, "b64_png": base64.b64encode(img_bytes).decode("ascii")}


# ─── GLOBAL LIVE CHAT (NO API KEYS) ─────────────────────────────────────
CHAT_MAX = 250
CHAT_ROOMS: dict[str, list[dict]] = {"global": []}

def _clean_room(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9_\-]", "", s)
    return s or "global"

def _clean_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\r\n\t]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "Guest"
    return s[:24]

class ConnectionManager:
    def __init__(self):
        self.rooms: dict[str, set[WebSocket]] = {}

    async def connect(self, room: str, ws: WebSocket):
        await ws.accept()
        self.rooms.setdefault(room, set()).add(ws)

    def disconnect(self, room: str, ws: WebSocket):
        try:
            self.rooms.get(room, set()).discard(ws)
            if room in self.rooms and not self.rooms[room]:
                del self.rooms[room]
        except Exception:
            pass

    async def broadcast(self, room: str, payload: dict):
        dead = []
        for ws in list(self.rooms.get(room, set())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(room, ws)

manager = ConnectionManager()

def _chat_store(room: str, msg: dict):
    CHAT_ROOMS.setdefault(room, [])
    CHAT_ROOMS[room].append(msg)
    if len(CHAT_ROOMS[room]) > CHAT_MAX:
        CHAT_ROOMS[room] = CHAT_ROOMS[room][-CHAT_MAX:]

@app.get("/chat/history")
def chat_history(room: str = "global"):
    r = _clean_room(room)
    return {"room": r, "messages": CHAT_ROOMS.get(r, [])}

@app.post("/chat/post")
async def chat_post(request: Request):
    # Optional HTTP fallback (still no keys)
    data = await request.json()
    room = _clean_room(data.get("room") or "global")
    name = _clean_name(data.get("name") or "Guest")
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")
    msg = {"t": int(time.time() * 1000), "name": name, "text": text, "room": room}
    _chat_store(room, msg)
    await manager.broadcast(room, {"type": "msg", "msg": msg})
    return {"ok": True}

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    room = _clean_room(ws.query_params.get("room") or "global")
    name = _clean_name(ws.query_params.get("name") or "Guest")

    await manager.connect(room, ws)

    # announce join
    join_msg = {"t": int(time.time() * 1000), "name": "SYSTEM", "text": f"{name} joined.", "room": room}
    _chat_store(room, join_msg)
    await manager.broadcast(room, {"type": "msg", "msg": join_msg})

    try:
        while True:
            data = await ws.receive_text()
            try:
                obj = json.loads(data) if data and data.strip().startswith("{") else {"text": data}
            except Exception:
                obj = {"text": data}

            text = (obj.get("text") or "").strip()
            if not text:
                continue

            msg = {"t": int(time.time() * 1000), "name": name, "text": text[:2000], "room": room}
            _chat_store(room, msg)

            # optional log (no crash if vault missing)
            try:
                await asyncio.to_thread(
                    log_event,
                    session_id=f"chat:{room}",
                    role="user",
                    source="live_chat",
                    content=f"{name}: {msg['text']}",
                    meta={"room": room},
                )
            except Exception:
                pass

            await manager.broadcast(room, {"type": "msg", "msg": msg})

    except WebSocketDisconnect:
        manager.disconnect(room, ws)
    except Exception:
        manager.disconnect(room, ws)

    # announce leave (best effort)
    leave_msg = {"t": int(time.time() * 1000), "name": "SYSTEM", "text": f"{name} left.", "room": room}
    _chat_store(room, leave_msg)
    try:
        await manager.broadcast(room, {"type": "msg", "msg": leave_msg})
    except Exception:
        pass


# ─── ROUTES ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "css_version": int(time.time())})

@app.get("/health")
def health():
    return {"status": "ok", "memory_vault": ("enabled" if BM_LOGGER_OK else "disabled")}


# ─── ASK ───────────────────────────────────────────────────────────────
@app.post("/ask")
async def ask(request: Request):
    ip = request.client.host
    check_rate(ip)

    data = await request.json()
    sid = bm_session_id(request, data)

    user_prompt = (data.get("prompt") or "").strip()
    history = tail_history(data.get("history") or [], n=60)
    detail_mode = bool(data.get("detailMode", False))

    if not user_prompt:
        return {"openai": "Empty", "gemini": "Empty", "claude": "Empty"}

    openai_key, gemini_key, claude_key = get_keys_from_request(request)

    try:
        await asyncio.to_thread(
            log_event,
            session_id=sid,
            role="user",
            source="ui",
            content=user_prompt,
            meta={"route": "/ask", "detailMode": detail_mode},
        )
    except Exception:
        pass

    user_only = only_user_history(history)
    direct_prompt = build_direct_prompt(user_only, user_prompt)

    async def run_openai():
        try:
            return await asyncio.to_thread(openai_chat, openai_key, direct_prompt, detail_mode, "normal")
        except Exception as e:
            return f"OpenAI error: {type(e).__name__}: {e}"

    async def run_gemini():
        try:
            return await asyncio.to_thread(gem_generate, gemini_key, detail_mode, direct_prompt, "normal")
        except Exception as e:
            return f"Gemini error: {type(e).__name__}: {e}"

    async def run_claude():
        try:
            return await call_claude_async(claude_key, direct_prompt, detail_mode, "normal")
        except Exception as e:
            return f"Claude error: {type(e).__name__}: {e}"

    oai_text, gem_text, claude_text = await asyncio.gather(run_openai(), run_gemini(), run_claude())

    try:
        await asyncio.to_thread(log_event, session_id=sid, role="model:openai", source="openai", content=oai_text, meta={"route": "/ask", "detailMode": detail_mode, "model": OPENAI_MODEL})
        await asyncio.to_thread(log_event, session_id=sid, role="model:gemini", source="gemini", content=gem_text, meta={"route": "/ask", "detailMode": detail_mode, "model": (GEMINI_MODEL or "auto")})
        await asyncio.to_thread(log_event, session_id=sid, role="model:claude", source="claude", content=claude_text, meta={"route": "/ask", "detailMode": detail_mode, "model": CLAUDE_MODEL})
    except Exception:
        pass

    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now()}]\n")
            f.write("MODE: NORMAL_DIRECT\n")
            f.write(f"DETAIL_MODE: {detail_mode}\n")
            f.write(f"USER: {user_prompt}\n")
            f.write(f"ChatGPT: {oai_text}\n")
            f.write(f"Gemini: {gem_text}\n")
            f.write(f"Claude: {claude_text}\n")
    except Exception:
        pass

    return {"openai": oai_text, "gemini": gem_text, "claude": claude_text}


# ─── AUTO ROUND ────────────────────────────────────────────────────────
@app.post("/auto_round")
async def auto_round(req: Request):
    data = await req.json()
    sid = bm_session_id(req, data)

    history = tail_history(data.get("history") or [], n=80)
    detail_mode = bool(data.get("detailMode", False))

    openai_key, gemini_key, claude_key = get_keys_from_request(req)

    AUTO_STATE["running"] = True
    speaker = next_speaker()

    auto_prompt = build_room_prompt(
        history,
        user_prompt=None,
        extra="AUTO MODE: Read the ROOM TRANSCRIPT and respond to what others said. Stay on-topic."
    )

    if speaker == 0:
        text = await asyncio.to_thread(openai_chat, openai_key, auto_prompt, detail_mode, "auto")
        try:
            await asyncio.to_thread(log_event, session_id=sid, role="model:openai", source="openai", content=text, meta={"route": "/auto_round", "detailMode": detail_mode, "model": OPENAI_MODEL})
        except Exception:
            pass
        return {"speaker": "openai", "text": text}

    if speaker == 1:
        text = await asyncio.to_thread(gem_generate, gemini_key, detail_mode, auto_prompt, "auto")
        try:
            await asyncio.to_thread(log_event, session_id=sid, role="model:gemini", source="gemini", content=text, meta={"route": "/auto_round", "detailMode": detail_mode, "model": (GEMINI_MODEL or "auto")})
        except Exception:
            pass
        return {"speaker": "gemini", "text": text}

    text = await call_claude_async(claude_key, auto_prompt, detail_mode, "auto")
    try:
        await asyncio.to_thread(log_event, session_id=sid, role="model:claude", source="claude", content=text, meta={"route": "/auto_round", "detailMode": detail_mode, "model": CLAUDE_MODEL})
    except Exception:
        pass
    return {"speaker": "claude", "text": text}

@app.post("/auto_stop")
async def auto_stop():
    AUTO_STATE["running"] = False
    return {"ok": True}


# ─── BOARD DECISION ────────────────────────────────────────────────────
@app.post("/board_decision")
async def board_decision(request: Request):
    ip = request.client.host
    check_rate(ip)

    data = await request.json()
    sid = bm_session_id(request, data)

    history = tail_history(data.get("history") or [], n=120)
    detail_mode = bool(data.get("detailMode", False))
    room = format_room(history)

    openai_key, _, _ = get_keys_from_request(request)

    rules = (
        "Output exactly ONE short sentence.\n"
        "No extra text before/after.\n"
        "If not enough info: BOARD DECISION: Need more context."
    )
    if detail_mode:
        rules = (
            "Output starts with 'BOARD DECISION:' then 2–6 bullet points max.\n"
            "No extra text before 'BOARD DECISION:'."
        )

    prompt = (
        f"{ROOM_ROSTER}\n"
        "ROOM TRANSCRIPT:\n"
        f"{room if room else '[no messages yet]'}\n\n"
        f"RULES:\n{rules}"
    ).strip()

    try:
        text = await asyncio.to_thread(openai_referee, openai_key, prompt, detail_mode)
    except Exception as e:
        text = f"BOARD DECISION: Referee error: {type(e).__name__}: {e}"

    if not text.strip().startswith("BOARD DECISION:"):
        text = "BOARD DECISION: " + text.strip()

    try:
        await asyncio.to_thread(
            log_event,
            session_id=sid,
            role="model:referee",
            source="openai_referee",
            content=text,
            meta={"route": "/board_decision", "detailMode": detail_mode, "model": OPENAI_MODEL},
        )
    except Exception:
        pass

    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now()}]\n")
            f.write("MODE: BOARD_DECISION\n")
            f.write(f"DETAIL_MODE: {detail_mode}\n")
            f.write(f"{text}\n")
    except Exception:
        pass

    return {"board": text}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
