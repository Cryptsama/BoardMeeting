# music_routes.py — BoardMeeting Music (ComfyUI ACE-Step)
import os, json, time, uuid, random, base64, mimetypes, urllib.request, urllib.parse
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

router = APIRouter()

DEFAULT_COMFY = "https://comfyui.boardmeetingai.app"
DEFAULT_WORKFLOW = "workflows/ACE HIP HOP.json"
AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac")


def _get_base_url() -> str:
    return (
        (os.getenv("COMFYUI_BASE_URL") or "").strip()
        or (os.getenv("COMFY_URL") or "").strip()
        or (os.getenv("COMFY_BASE_URL") or "").strip()
        or (os.getenv("COMFYUI_URL") or "").strip()
        or DEFAULT_COMFY
    ).rstrip("/")


def _get_workflow_path() -> str:
    return (
        (os.getenv("ACE_WORKFLOW_PATH") or "").strip()
        or (os.getenv("ACE_AUDIO_WORKFLOW") or "").strip()
        or DEFAULT_WORKFLOW
    )


def _get_out_dir() -> Path:
    out = Path((os.getenv("BM_MUSIC_OUTDIR") or "outputs_music").strip() or "outputs_music")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _cf_headers() -> dict:
    h = {}
    cid = (os.getenv("CF_ACCESS_CLIENT_ID") or "").strip()
    csec = (os.getenv("CF_ACCESS_CLIENT_SECRET") or "").strip()
    if cid and csec:
        h["CF-Access-Client-Id"] = cid
        h["CF-Access-Client-Secret"] = csec
    return h


def _http_json(method: str, url: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = None
    headers = {"Content-Type": "application/json", **_cf_headers()}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            preview = raw[:800].decode("utf-8", "ignore")
            raise HTTPException(status_code=502, detail=f"ComfyUI non-JSON response from {url}: {preview}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI request failed: {e}")


def _http_bytes(url: str, timeout: int = 60) -> bytes:
    headers = {**_cf_headers()}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ComfyUI download failed: {e}")


def _load_workflow() -> dict:
    p = Path(_get_workflow_path())
    if not p.exists():
        raise HTTPException(status_code=500, detail=f"Workflow file not found: {p.resolve()}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Workflow JSON invalid: {e}")


def _patch_ace_workflow(
    wf: dict,
    *,
    tags: str,
    seconds: float,
    seed: int,
    steps: int,
    cfg: float,
    sampler: str,
    denoise: float,
    filename_prefix: str,
):
    # workflow node IDs
    if "14" in wf and "inputs" in wf["14"]:
        wf["14"]["inputs"]["tags"] = tags
    if "17" in wf and "inputs" in wf["17"]:
        wf["17"]["inputs"]["seconds"] = float(seconds)
    if "52" in wf and "inputs" in wf["52"]:
        wf["52"]["inputs"]["seed"] = int(seed)
        wf["52"]["inputs"]["steps"] = int(steps)
        wf["52"]["inputs"]["cfg"] = float(cfg)
        wf["52"]["inputs"]["sampler_name"] = str(sampler)
        wf["52"]["inputs"]["denoise"] = float(denoise)
    if "59" in wf and "inputs" in wf["59"]:
        wf["59"]["inputs"]["filename_prefix"] = filename_prefix


def _find_first_audio_anywhere(history_item: dict) -> tuple[str, str, str] | None:
    found = None

    def walk(x):
        nonlocal found
        if found is not None:
            return
        if isinstance(x, dict):
            fn = x.get("filename")
            if isinstance(fn, str) and fn.lower().endswith(AUDIO_EXTS):
                found = (fn, (x.get("subfolder") or ""), (x.get("type") or "output"))
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(history_item)
    return found


@router.get("/health")
def music_health():
    base = _get_base_url()
    wf = Path(_get_workflow_path())
    out = _get_out_dir()
    return {
        "ok": True,
        "comfyui": base,
        "workflow": str(wf.resolve()),
        "outdir": str(out.resolve()),
        "cloudflare_headers_enabled": bool(_cf_headers()),
    }


@router.post("/generate")
def music_generate(payload: dict):
    tags = str(
        payload.get("tags")
        or payload.get("prompt")
        or payload.get("text")
        or payload.get("lyrics")
        or payload.get("music_prompt")
        or ""
    ).strip()
    if not tags:
        raise HTTPException(status_code=400, detail="Missing 'tags' (your prompt).")

    seconds = float(payload.get("seconds", 30.0))
    steps = int(payload.get("steps", 50))
    cfg = float(payload.get("cfg", 5.0))
    sampler = str(payload.get("sampler_name", "euler"))
    denoise = float(payload.get("denoise", 1.0))
    seed_in = payload.get("seed", None)
    seed = int(seed_in) if seed_in not in (None, "", "random") else random.randint(1, 2_000_000_000)

    job_id = uuid.uuid4().hex[:10]
    client_id = uuid.uuid4().hex
    filename_prefix = f"boardmeeting_music_{job_id}"

    base = _get_base_url()
    wf = _load_workflow()

    _patch_ace_workflow(
        wf,
        tags=tags,
        seconds=seconds,
        seed=seed,
        steps=steps,
        cfg=cfg,
        sampler=sampler,
        denoise=denoise,
        filename_prefix=filename_prefix,
    )

    r = _http_json("POST", f"{base}/prompt", {"prompt": wf, "client_id": client_id}, timeout=60)
    prompt_id = r.get("prompt_id")
    if not prompt_id:
        raise HTTPException(status_code=502, detail=f"ComfyUI did not return prompt_id: {r}")

    t0 = time.time()
    timeout_s = int(payload.get("timeout_s", 240))

    while time.time() - t0 < timeout_s:
        hist = _http_json("GET", f"{base}/history/{prompt_id}", timeout=60) or {}
        item = hist.get(prompt_id)
        if item is None and isinstance(hist, dict) and len(hist) == 1:
            item = next(iter(hist.values()), None)

        if isinstance(item, dict):
            outinfo = _find_first_audio_anywhere(item)
            if outinfo:
                filename, subfolder, ftype = outinfo

                qs = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": ftype})
                file_bytes = _http_bytes(f"{base}/view?{qs}", timeout=120)

                ext = Path(filename).suffix.lower() or ".mp3"
                if ext not in AUDIO_EXTS:
                    ext = ".mp3"

                out_dir = _get_out_dir()
                local_name = f"boardmeeting_{job_id}{ext}"
                local_path = out_dir / local_name
                local_path.write_bytes(file_bytes)

                url = f"/music/file/{local_name}"
                mime = mimetypes.guess_type(local_name)[0] or ("audio/mpeg" if ext == ".mp3" else "audio/wav")
                b64 = base64.b64encode(file_bytes).decode("ascii")
                data_url = f"data:{mime};base64,{b64}"

                # return ALL common key names so the frontend always finds it
                return {
                    "ok": True,
                    "prompt_id": prompt_id,
                    "seed": seed,
                    "file": local_name,
                    "filename": local_name,
                    "mime": mime,

                    # urls (snake + camel)
                    "url": url,
                    "src": url,
                    "audio": url,
                    "audio_url": url,
                    "audioUrl": url,
                    "audio_src": url,
                    "audioSrc": url,
                    "file_url": url,
                    "fileUrl": url,
                    "download_url": url,
                    "downloadUrl": url,
                    "download": url,

                    # IMPORTANT: what your frontend expects
                    "b64_audio": b64,
                    "b64Audio": b64,

                    # data-url (snake + camel)
                    "audio_base64": b64,
                    "audioBase64": b64,
                    "audio_data_url": data_url,
                    "audioDataUrl": data_url,
                    "data_url": data_url,
                    "dataUrl": data_url,
                }

        time.sleep(1.0)

    raise HTTPException(status_code=504, detail=f"Timed out waiting for ComfyUI result (prompt_id={prompt_id}).")


@router.get("/file/{name}")
def music_file(name: str):
    out_dir = _get_out_dir()
    p = (out_dir / name).resolve()
    if not str(p).startswith(str(out_dir.resolve())) or not p.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    mime = mimetypes.guess_type(str(p))[0] or ("audio/mpeg" if p.suffix.lower() == ".mp3" else "application/octet-stream")
    return FileResponse(str(p), media_type=mime, filename=name)
