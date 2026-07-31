import sys
import io
# Force UTF-8 output on Windows terminals
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from fastapi import FastAPI, UploadFile, File, Query, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import os
import re
import shutil
import subprocess
import json
import zipfile
import io
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from pydantic import BaseModel


# === CONFIGURATION ===
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024   # 2 GB
CHUNK_COPY_BUFFER = 8 * 1024 * 1024         # 8 MB copy buffer
WORKER_COUNT = min(8, (os.cpu_count() or 4) * 2)

app = FastAPI(title="⚡ Ultra-Fast Auto Shorts Generator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
INDEX_HTML_PATH = FRONTEND_DIR / "index_simple.html"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Absolute path to ffmpeg
FFMPEG_PATH = r"C:\Users\hp\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
FFPROBE_PATH = FFMPEG_PATH.replace("ffmpeg.exe", "ffprobe.exe")

# In-memory progress store — avoids per-clip disk I/O
_progress_lock = threading.Lock()
_progress: dict[str, dict] = {}

# Thread executor for parallel clip generation
clip_executor = ThreadPoolExecutor(max_workers=WORKER_COUNT)

print(f"\n{'='*60}")
print("[FAST] ULTRA-FAST SHORTS GENERATOR")
print(f"{'='*60}")
print(f"[OK] Max upload    : 2 GB")
print(f"[OK] Strategy      : Fixed-length clips (no scene detection)")
print(f"[OK] Encoding      : Stream copy (no re-encoding)")
print(f"[OK] Workers       : {WORKER_COUNT}")
print(f"[OK] Progress      : In-memory (no disk I/O)")
print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    """Strip directory components and dangerous characters."""
    name = os.path.basename(name)
    name = re.sub(r'[^\w\-_. ]', '_', name)
    return name or "upload"


def write_progress(task_id: str, percent: int, status_msg: str) -> None:
    with _progress_lock:
        _progress[task_id] = {
            "task_id": task_id,
            "status": status_msg,
            "percent": min(100, int(percent)),
        }


def get_video_duration(filepath: str) -> float:
    """Return video duration in seconds via ffprobe."""
    try:
        cmd = [
            FFPROBE_PATH, "-v", "quiet",
            "-print_format", "json",
            "-show_format", filepath,
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception as exc:
        print(f"   [WARNING] Duration detection failed: {exc}")
        return 0.0


def create_fixed_clips(total_duration: float, target_duration: int) -> list:
    """Divide total_duration into fixed-length chunks (pure math, no I/O)."""
    clips = []
    num_clips = int(total_duration / target_duration)
    for i in range(num_clips):
        start = i * target_duration
        end = start + target_duration
        clips.append({
            "start": start,
            "end": min(end, total_duration),
            "duration": target_duration if end <= total_duration else (total_duration - start),
        })
    return clips


def format_duration(seconds: float) -> str:
    """Format seconds → M:SS or H:MM:SS."""
    try:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
    except Exception:
        return "0:00"


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def home():
    if INDEX_HTML_PATH.exists():
        return FileResponse(INDEX_HTML_PATH, media_type="text/html; charset=utf-8")
    return {
        "message": "⚡ Ultra-Fast Shorts Generator",
        "max_upload_gb": 2,
        "strategy": "FFmpeg fixed clips + stream copy",
        "workers": WORKER_COUNT,
    }


@app.get("/api")
def api_info():
    return {
        "message": "⚡ Ultra-Fast Shorts Generator",
        "max_upload_gb": 2,
        "strategy": "FFmpeg fixed clips + stream copy",
        "workers": WORKER_COUNT,
    }


# ── Chunked upload ────────────────────────────────────────────────────────────

@app.post("/upload-chunk")
async def upload_chunk(
    chunk: UploadFile = File(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),      # noqa: F841 (kept for validation)
    filename: str = Form(...),
    upload_id: str = Form(...),
):
    """Receive a single chunk and persist it to a temp directory."""
    safe_name = safe_filename(filename)
    temp_dir = os.path.join(UPLOAD_FOLDER, f"tmp_{upload_id}")
    os.makedirs(temp_dir, exist_ok=True)

    chunk_path = os.path.join(temp_dir, f"chunk_{chunk_index}")
    try:
        with open(chunk_path, "wb") as f:
            # Stream-write the chunk in 2 MB sub-slices
            while data := await chunk.read(2 * 1024 * 1024):
                f.write(data)
        return {"status": "chunk_saved", "chunk_index": chunk_index, "filename": safe_name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save chunk: {exc}")


class MergeRequest(BaseModel):
    upload_id: str
    filename: str
    total_chunks: int


@app.post("/merge-chunks")
async def merge_chunks(req: MergeRequest):
    """Merge all chunks into the final file using buffered copy (no full RAM load)."""
    safe_name = safe_filename(req.filename)
    temp_dir = os.path.join(UPLOAD_FOLDER, f"tmp_{req.upload_id}")
    filepath = os.path.join(UPLOAD_FOLDER, safe_name)

    if not os.path.exists(temp_dir):
        raise HTTPException(status_code=404, detail="Upload temp directory not found.")

    try:
        with open(filepath, "wb") as target:
            for i in range(req.total_chunks):
                chunk_path = os.path.join(temp_dir, f"chunk_{i}")
                if not os.path.exists(chunk_path):
                    raise HTTPException(status_code=422, detail=f"Missing chunk {i}")
                with open(chunk_path, "rb") as src:
                    shutil.copyfileobj(src, target, CHUNK_COPY_BUFFER)

        # Clean up temp directory after successful merge
        shutil.rmtree(temp_dir, ignore_errors=True)

        size_gb = os.path.getsize(filepath) / (1024 ** 3)
        return {
            "status": "success",
            "filename": safe_name,
            "size_gb": round(size_gb, 2),
            "message": f"✅ Ready — {size_gb:.1f} GB uploaded",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to merge: {exc}")


# ── Shorts generation ─────────────────────────────────────────────────────────

@app.post("/auto-shorts")
def auto_generate_shorts(
    filename: str = Query(...),
    target_duration: int = Query(default=45, ge=5, le=600),
):
    """
    Generate fixed-length clips using FFmpeg stream copy (no re-encoding).
    Uses a thread pool for parallel clip creation.
    Progress is tracked in memory and served via /progress/{task_id}.
    """
    safe_name = safe_filename(filename)
    filepath = os.path.join(UPLOAD_FOLDER, safe_name)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"File not found: {safe_name}")

    task_id = os.path.splitext(safe_name)[0]
    write_progress(task_id, 0, "Reading metadata...")

    size_gb = os.path.getsize(filepath) / (1024 ** 3)
    print(f"\n[START] Generating {safe_name} ({size_gb:.1f} GB)")

    total_duration = get_video_duration(filepath)
    if total_duration <= 0:
        write_progress(task_id, 0, "Error reading video")
        raise HTTPException(status_code=422, detail="Could not read video duration")

    write_progress(task_id, 5, "Creating clip list...")
    clips = create_fixed_clips(total_duration, target_duration)
    if not clips:
        write_progress(task_id, 0, "Error creating clips")
        raise HTTPException(status_code=422, detail="No clips could be created")

    total_clips = len(clips)
    base_name = os.path.splitext(safe_name)[0]
    write_progress(task_id, 10, f"Encoding {total_clips} clips...")

    completed_count = 0
    count_lock = threading.Lock()

    def create_clip(i: int, clip: dict) -> dict:
        nonlocal completed_count
        short_name = f"{base_name}_short_{i+1:02d}.mp4"
        output_path = os.path.join(OUTPUT_FOLDER, short_name)

        cmd = [
            FFMPEG_PATH, "-y",
            "-ss", str(clip["start"]),
            "-i", filepath,
            "-t", str(clip["duration"]),
            "-c:v", "copy", "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path,
        ]

        proc = subprocess.run(
            cmd, capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=120,
        )

        with count_lock:
            completed_count += 1
            pct = 10 + int(completed_count / total_clips * 88)
            write_progress(task_id, pct, f"Clip {completed_count}/{total_clips}")

        if proc.returncode == 0 and os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            return {
                "index": i + 1,
                "filename": short_name,
                "duration": round(clip["duration"], 1),
                "duration_formatted": format_duration(clip["duration"]),
                "file_size_mb": round(size_mb, 2),
                "download_url": f"/download/{short_name}",
            }
        return {"index": i + 1, "filename": short_name, "error": "ffmpeg failed"}

    futures = {clip_executor.submit(create_clip, i, clip): i for i, clip in enumerate(clips)}
    created_shorts = [None] * total_clips
    for fut in as_completed(futures):
        idx = futures[fut]
        created_shorts[idx] = fut.result()

    success_count = sum(1 for s in created_shorts if s and "error" not in s)
    write_progress(task_id, 100, "Complete!")
    print(f"[DONE] {success_count}/{total_clips} clips created")

    return {
        "total_shorts": success_count,
        "source_video": safe_name,
        "file_size_gb": round(size_gb, 2),
        "total_duration_seconds": round(total_duration, 1),
        "shorts": [s for s in created_shorts if s],
    }


# ── Progress ──────────────────────────────────────────────────────────────────

@app.get("/progress/{task_id}")
def get_progress(task_id: str):
    """Return in-memory progress for a task (zero disk I/O)."""
    with _progress_lock:
        data = _progress.get(task_id)
    if data:
        return data
    return {"task_id": task_id, "status": "not_found", "percent": 0}


# ── File serving ──────────────────────────────────────────────────────────────

@app.get("/download/{filename}")
def download_short(filename: str):
    """Serve a single generated clip with proper Content-Length."""
    safe_name = safe_filename(filename)
    filepath = os.path.join(OUTPUT_FOLDER, safe_name)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"File not found: {safe_name}")
    return FileResponse(
        path=filepath,
        filename=safe_name,
        media_type="video/mp4",
        headers={"Content-Length": str(os.path.getsize(filepath))},
    )


@app.get("/list-shorts")
def list_shorts():
    """List all generated shorts with metadata."""
    files = []
    if os.path.exists(OUTPUT_FOLDER):
        for fname in sorted(os.listdir(OUTPUT_FOLDER)):
            if not fname.lower().endswith(".mp4"):
                continue
            path = os.path.join(OUTPUT_FOLDER, fname)
            size_mb = round(os.path.getsize(path) / (1024 * 1024), 2)
            files.append({
                "filename": fname,
                "file_size_mb": size_mb,
                "download_url": f"/download/{fname}",
            })
    return {"shorts": files, "count": len(files)}


@app.get("/download-all-zip")
def download_all_zip():
    """
    Stream a ZIP of all shorts without loading everything into RAM.
    Each file is streamed directly into the ZIP response buffer.
    """
    mp4_files = [
        f for f in os.listdir(OUTPUT_FOLDER)
        if f.lower().endswith(".mp4")
    ] if os.path.exists(OUTPUT_FOLDER) else []

    if not mp4_files:
        raise HTTPException(status_code=404, detail="No shorts available to download.")

    def zip_generator():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for fname in sorted(mp4_files):
                fpath = os.path.join(OUTPUT_FOLDER, fname)
                zf.write(fpath, arcname=fname)
                # Yield chunks as each file is added
                buf.seek(0)
                yield buf.read()
                buf.seek(0)
                buf.truncate(0)
        # Flush any remaining ZIP metadata
        buf.seek(0)
        remaining = buf.read()
        if remaining:
            yield remaining

    return StreamingResponse(
        zip_generator(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=shorts.zip"},
    )


@app.post("/clear-outputs")
def clear_outputs():
    """Delete all generated shorts."""
    try:
        deleted = 0
        if os.path.exists(OUTPUT_FOLDER):
            for f in os.listdir(OUTPUT_FOLDER):
                if f.lower().endswith(".mp4"):
                    os.remove(os.path.join(OUTPUT_FOLDER, f))
                    deleted += 1
        # Also clear in-memory progress entries
        with _progress_lock:
            _progress.clear()
        return {"status": "cleared", "files_deleted": deleted}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))