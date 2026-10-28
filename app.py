"""CIJEs 3DGEN — simple job queue + STL download API for an external generator bot."""

from __future__ import annotations

import json
import math
import os
import struct
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "jobs"
STATIC_DIR = BASE_DIR / "static"


class Settings(BaseSettings):
    root_path: str = ""
    upload_token: str = "change-me"
    bot_webhook_url: str = ""
    bot_webhook_key: str = ""
    local_generator: int = 0
    host: str = "127.0.0.1"
    port: int = 8090

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
# Normalize ROOT_PATH: strip trailing slash, ensure leading slash if non-empty
_rp = (settings.root_path or "").strip().rstrip("/")
if _rp and not _rp.startswith("/"):
    _rp = "/" + _rp
settings.root_path = _rp

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="CIJEs 3DGEN",
    description="Queue prompts for CIJEs 3DGEN model generation and download STL files.",
    root_path=settings.root_path or "",
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=500)


class JobMeta(BaseModel):
    id: str
    prompt: str
    status: str  # queued | generating | ready | failed
    created_at: str
    updated_at: str
    error: Optional[str] = None
    filename: Optional[str] = None
    awaiting_custom: bool = False  # no local template matched; waiting for an external model


# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str) -> Path:
    return DATA_DIR / job_id


def _meta_path(job_id: str) -> Path:
    return _job_dir(job_id) / "meta.json"


def _model_path(job_id: str) -> Path:
    return _job_dir(job_id) / "model.stl"


def _load_meta(job_id: str) -> JobMeta:
    path = _meta_path(job_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Job not found")
    with path.open("r", encoding="utf-8") as f:
        return JobMeta(**json.load(f))


def _save_meta(meta: JobMeta) -> None:
    d = _job_dir(meta.id)
    d.mkdir(parents=True, exist_ok=True)
    meta.updated_at = _now_iso()
    with _meta_path(meta.id).open("w", encoding="utf-8") as f:
        json.dump(meta.model_dump(), f, indent=2)


def _job_public(meta: JobMeta) -> dict[str, Any]:
    return {
        "id": meta.id,
        "prompt": meta.prompt,
        "status": meta.status,
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "error": meta.error,
        "download_ready": meta.status == "ready" and _model_path(meta.id).is_file(),
        "awaiting_custom": bool(meta.awaiting_custom) and meta.status not in ("ready", "failed"),
    }


# ---------------------------------------------------------------------------
# Binary STL writer (no numpy-stl dependency)
# ---------------------------------------------------------------------------


def _write_binary_stl(path: Path, triangles: list[tuple]) -> None:
    """Write a binary STL from a list of ((nx,ny,nz), (v0,v1,v2)) triangles.

    Each vertex is (x, y, z). Normal may be (0,0,0) and will be recomputed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = b"CIJEs 3DGEN local generator".ljust(80, b"\0")
    buf = bytearray(header)
    buf += struct.pack("<I", len(triangles))
    for normal, (v0, v1, v2) in triangles:
        nx, ny, nz = normal
        if nx == 0 and ny == 0 and nz == 0:
            # Compute face normal
            ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
            bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
            nx = ay * bz - az * by
            ny = az * bx - ax * bz
            nz = ax * by - ay * bx
            length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / length, ny / length, nz / length
        buf += struct.pack("<3f", nx, ny, nz)
        for v in (v0, v1, v2):
            buf += struct.pack("<3f", float(v[0]), float(v[1]), float(v[2]))
        buf += struct.pack("<H", 0)
    path.write_bytes(buf)


def _quad(a, b, c, d) -> list:
    """Two triangles for a quad a-b-c-d (CCW when viewed from outside)."""
    return [((0, 0, 0), (a, b, c)), ((0, 0, 0), (a, c, d))]


def _box(cx, cy, cz, sx, sy, sz) -> list:
    """Axis-aligned box centered at (cx,cy,cz) with size (sx,sy,sz)."""
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    # 8 corners
    p = [
        (cx - hx, cy - hy, cz - hz),  # 0
        (cx + hx, cy - hy, cz - hz),  # 1
        (cx + hx, cy + hy, cz - hz),  # 2
        (cx - hx, cy + hy, cz - hz),  # 3
        (cx - hx, cy - hy, cz + hz),  # 4
        (cx + hx, cy - hy, cz + hz),  # 5
        (cx + hx, cy + hy, cz + hz),  # 6
        (cx - hx, cy + hy, cz + hz),  # 7
    ]
    tris = []
    # -Z, +Z, -Y, +Y, -X, +X
    tris += _quad(p[0], p[3], p[2], p[1])
    tris += _quad(p[4], p[5], p[6], p[7])
    tris += _quad(p[0], p[1], p[5], p[4])
    tris += _quad(p[3], p[7], p[6], p[2])
    tris += _quad(p[0], p[4], p[7], p[3])
    tris += _quad(p[1], p[2], p[6], p[5])
    return tris


def _cylinder(cx, cy, cz, radius, height, segments=32) -> list:
    """Vertical cylinder along Y, centered at (cx,cy,cz)."""
    tris = []
    hy = height / 2
    top_c = (cx, cy + hy, cz)
    bot_c = (cx, cy - hy, cz)
    ring_t = []
    ring_b = []
    for i in range(segments):
        a = 2 * math.pi * i / segments
        x = cx + radius * math.cos(a)
        z = cz + radius * math.sin(a)
        ring_t.append((x, cy + hy, z))
        ring_b.append((x, cy - hy, z))
    for i in range(segments):
        j = (i + 1) % segments
        # side
        tris += _quad(ring_b[i], ring_b[j], ring_t[j], ring_t[i])
        # top (CCW from above)
        tris.append(((0, 0, 0), (top_c, ring_t[i], ring_t[j])))
        # bottom (CCW from below = reverse)
        tris.append(((0, 0, 0), (bot_c, ring_b[j], ring_b[i])))
    return tris


def _torus_approx(cx, cy, cz, R, r, seg_major=24, seg_minor=12) -> list:
    """Simple torus approximation (for yoyo look — outer rim)."""
    tris = []

    def pt(i, j):
        u = 2 * math.pi * i / seg_major
        v = 2 * math.pi * j / seg_minor
        x = cx + (R + r * math.cos(v)) * math.cos(u)
        y = cy + r * math.sin(v)
        z = cz + (R + r * math.cos(v)) * math.sin(u)
        return (x, y, z)

    for i in range(seg_major):
        for j in range(seg_minor):
            ni, nj = (i + 1) % seg_major, (j + 1) % seg_minor
            a, b, c, d = pt(i, j), pt(ni, j), pt(ni, nj), pt(i, nj)
            tris += _quad(a, b, c, d)
    return tris


def _generate_local_stl(prompt: str, out_path: Path) -> bool:
    """Generate a simple STL for known keywords. Returns True if matched."""
    p = prompt.lower().strip()
    tris: list = []

    if "yoyo" in p or "yo-yo" in p or "yo yo" in p:
        # Two discs + axle
        tris += _cylinder(0, 6, 0, 25, 6, 36)
        tris += _cylinder(0, -6, 0, 25, 6, 36)
        tris += _cylinder(0, 0, 0, 4, 14, 16)
        # slight outer rim bulge via thin torus-ish rings
        tris += _torus_approx(0, 6, 0, 22, 2.5, 24, 8)
        tris += _torus_approx(0, -6, 0, 22, 2.5, 24, 8)
    elif "phone" in p or "stand" in p:
        # Phone stand: base + angled backrest (approximated as boxes)
        tris += _box(0, 2, 0, 60, 4, 40)  # base
        # backrest tilted ~60° — approximate with thin box standing up
        tris += _box(0, 25, -12, 55, 50, 4)
        # lip at front
        tris += _box(0, 6, 16, 55, 8, 4)
    elif "cylinder" in p or "tube" in p or "pipe" in p:
        tris += _cylinder(0, 0, 0, 15, 40, 32)
    elif "disc" in p or "disk" in p or "coin" in p:
        tris += _cylinder(0, 0, 0, 20, 3, 48)
    elif "cube" in p or "box" in p or "square" in p:
        tris += _box(0, 0, 0, 30, 30, 30)
    else:
        return False

    _write_binary_stl(out_path, tris)
    return True


def _run_local_generator(job_id: str) -> None:
    """Background: generate STL locally if prompt matches known keywords."""
    try:
        meta = _load_meta(job_id)
        meta.status = "generating"
        _save_meta(meta)
        out = _model_path(job_id)
        ok = _generate_local_stl(meta.prompt, out)
        if ok and out.is_file():
            meta = _load_meta(job_id)
            meta.status = "ready"
            meta.awaiting_custom = False
            meta.filename = "model.stl"
            meta.error = None
            _save_meta(meta)
        else:
            # Unknown prompts are intentionally left active for the external
            # generator/bot to handle later. Keep polling instead of reporting
            # an immediate failure, and do not expose a stale error message.
            meta = _load_meta(job_id)
            if meta.status != "ready":  # an upload may have landed meanwhile
                meta.status = "queued"
                meta.awaiting_custom = True
                meta.error = None
                _save_meta(meta)
    except Exception as exc:  # noqa: BLE001
        try:
            meta = _load_meta(job_id)
            meta.status = "failed"
            meta.error = f"Local generator error: {exc}"
            _save_meta(meta)
        except Exception:  # noqa: BLE001
            pass


def _notify_bot(job_id: str, prompt: str) -> None:
    url = (settings.bot_webhook_url or "").strip()
    if not url:
        return
    headers = {"Content-Type": "application/json"}
    key = (settings.bot_webhook_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-Webhook-Key"] = key
    payload = {
        "job_id": job_id,
        "prompt": prompt,
        "callback_hint": f"PUT /api/jobs/{job_id}/model with header X-Upload-Token",
    }

    def _post() -> None:
        try:
            with httpx.Client(timeout=30.0) as client:
                client.post(url, json=payload, headers=headers)
            meta = _load_meta(job_id)
            if meta.status == "queued":
                meta.status = "generating"
                _save_meta(meta)
        except Exception:  # noqa: BLE001
            # Leave job queued; bot/admin can still upload later
            pass

    threading.Thread(target=_post, daemon=True).start()


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.get("/health")
@app.get("/ready")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "3dmaker",
        "local_generator": bool(settings.local_generator),
        "webhook_configured": bool(settings.bot_webhook_url),
    }


@app.post("/api/jobs")
async def create_job(body: CreateJobRequest) -> JSONResponse:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    if len(prompt) > 500:
        raise HTTPException(status_code=400, detail="Prompt max length is 500 characters")

    job_id = str(uuid.uuid4())
    now = _now_iso()
    meta = JobMeta(
        id=job_id,
        prompt=prompt,
        status="queued",
        created_at=now,
        updated_at=now,
    )
    _save_meta(meta)

    if settings.local_generator:
        threading.Thread(target=_run_local_generator, args=(job_id,), daemon=True).start()
    else:
        _notify_bot(job_id, prompt)

    return JSONResponse(_job_public(meta), status_code=201)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    meta = _load_meta(job_id)
    return _job_public(meta)


@app.put("/api/jobs/{job_id}/model")
async def upload_model(
    job_id: str,
    file: UploadFile = File(...),
    x_upload_token: Optional[str] = Header(None, alias="X-Upload-Token"),
) -> dict[str, Any]:
    expected = settings.upload_token
    if not x_upload_token or not expected or x_upload_token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Upload-Token")

    meta = _load_meta(job_id)
    # Accept STL (or any binary); store as model.stl
    dest = _model_path(job_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    if len(content) < 84:
        raise HTTPException(status_code=400, detail="File too small to be a valid STL")
    if len(content) > 80 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 80MB)")
    dest.write_bytes(content)

    meta.status = "ready"
    meta.awaiting_custom = False
    meta.filename = "model.stl"
    meta.error = None
    _save_meta(meta)
    return _job_public(meta)


@app.get("/api/jobs/{job_id}/download")
def download_model(job_id: str) -> FileResponse:
    meta = _load_meta(job_id)
    path = _model_path(job_id)
    if meta.status != "ready" or not path.is_file():
        raise HTTPException(status_code=404, detail="Model not ready")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in meta.prompt[:40])
    filename = f"{safe_name or 'model'}.stl"
    return FileResponse(
        path,
        media_type="application/sla",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.middleware("http")
async def _no_store(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if "/api/" in path and not path.endswith("/download") or path.endswith((".js", ".html", ".css", "/")):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


# Static UI last so API routes take precedence
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
