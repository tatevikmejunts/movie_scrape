"""FastAPI web UI for downloading videos from kinogomy.net title pages.

Run locally:
    .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://<your-computer-ip>:8000 on your phone (same Wi-Fi).
"""

import threading
import time
from pathlib import Path

import download_video as dv
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Kinogomy Downloader")
templates = Jinja2Templates(directory="templates")

downloads_dir = Path("downloads")
downloads_dir.mkdir(exist_ok=True)

# In-memory job store. Restarts erase it; files remain in downloads/.
download_jobs: dict[str, dict] = {}


def fetch_episode_data(page_url: str) -> dict:
    """Return iframe URL and flat episode list for a title page."""
    html = dv.fetch_text(page_url)
    soup = BeautifulSoup(html, "html.parser")
    iframe = dv.find_player_iframe(soup, page_url)
    if not iframe:
        return {"error": "No player iframe found on the page."}

    iframe_html = dv.fetch_text(iframe, referer=page_url)
    seasons = dv.parse_json_array(iframe_html, "seasons")
    streams = dv.extract_streams(seasons)
    return {"iframe": iframe, "streams": streams}


def run_download(job_id: str, stream: dict, out_path: Path, lang: str) -> None:
    """Background worker that downloads one episode."""
    try:
        download_jobs[job_id]["status"] = "downloading"
        audio_index = dv.audio_index_for_language(stream["audio_names"], lang)
        map_args = None
        if audio_index is not None:
            map_args = ["-map", "0:v:0", "-map", f"0:a:{audio_index}"]
        success = dv.run_ffmpeg(stream["hls"], out_path, map_args=map_args)
        download_jobs[job_id]["status"] = "done" if success else "failed"
    except Exception as exc:  # noqa: BLE001
        download_jobs[job_id]["status"] = f"failed: {exc}"


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "default_url": dv.DEFAULT_URL},
    )


@app.post("/episodes")
def episodes(page_url: str = Form(...)) -> JSONResponse:
    try:
        data = fetch_episode_data(page_url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)

    if "error" in data:
        return JSONResponse(data, status_code=400)

    grouped: dict[str, list[dict]] = {}
    for stream in data["streams"]:
        key = f"Season {stream['season']}"
        grouped.setdefault(key, []).append({
            "season": stream["season"],
            "episode": stream["episode"],
            "title": stream["title"],
            "audio": stream["audio_names"],
        })

    return JSONResponse({
        "iframe": data["iframe"],
        "seasons": sorted(grouped.keys(), key=lambda s: int(s.split()[1])),
        "episodes": grouped,
    })


def queue_download(
    page_title: str | None,
    page_url: str,
    stream: dict,
    lang: str,
) -> str:
    """Queue a single episode download and return its job ID."""
    season = stream["season"]
    episode = stream["episode"]
    out_path = dv.make_out_path(page_title, page_url, None, season, episode)
    out_path = downloads_dir / out_path.name

    job_id = f"{season}-{episode}-{lang}-{int(time.time() * 1000)}"
    download_jobs[job_id] = {
        "status": "starting",
        "path": str(out_path),
        "season": season,
        "episode": episode,
        "lang": lang,
        "filename": out_path.name,
    }

    threading.Thread(
        target=run_download,
        args=(job_id, stream, out_path, lang),
        daemon=True,
    ).start()
    return job_id


@app.post("/download")
def start_download(
    page_url: str = Form(...),
    season: int = Form(...),
    episode: int = Form(...),
    lang: str = Form("eng"),
) -> JSONResponse:
    try:
        data = fetch_episode_data(page_url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)

    if "error" in data:
        return JSONResponse(data, status_code=400)

    stream = dv.pick_stream(data["streams"], season, episode, lang)
    if not stream:
        return JSONResponse(
            {"error": f"No stream found for S{season:02d}E{episode:02d}"},
            status_code=404,
        )

    html = dv.fetch_text(page_url)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("h1")
    page_title = title_tag.get_text(strip=True) if title_tag else None

    job_id = queue_download(page_title, page_url, stream, lang)
    return JSONResponse({
        "job_id": job_id,
        "filename": download_jobs[job_id]["filename"],
        "status": "starting",
    })


@app.post("/download-batch")
def start_batch_download(
    page_url: str = Form(...),
    season: int = Form(...),
    episode_from: int = Form(...),
    episode_to: int = Form(...),
    lang: str = Form("eng"),
) -> JSONResponse:
    try:
        data = fetch_episode_data(page_url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)

    if "error" in data:
        return JSONResponse(data, status_code=400)

    html = dv.fetch_text(page_url)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("h1")
    page_title = title_tag.get_text(strip=True) if title_tag else None

    job_ids = []
    start = min(episode_from, episode_to)
    end = max(episode_from, episode_to)
    for episode in range(start, end + 1):
        stream = dv.pick_stream(data["streams"], season, episode, lang)
        if stream:
            job_id = queue_download(page_title, page_url, stream, lang)
            job_ids.append(job_id)

    if not job_ids:
        return JSONResponse(
            {"error": f"No episodes found for S{season:02d}E{start:02d}-E{end:02d}"},
            status_code=404,
        )

    return JSONResponse({
        "job_ids": job_ids,
        "count": len(job_ids),
        "status": "starting",
    })


@app.get("/status/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    return JSONResponse(download_jobs.get(job_id, {"status": "not found"}))


@app.get("/files")
def list_files() -> JSONResponse:
    files = []
    for path in sorted(downloads_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        files.append({
            "name": path.name,
            "size": path.stat().st_size,
        })
    return JSONResponse({"files": files})


@app.get("/files/{filename}", response_model=None)
def serve_file(filename: str):
    file_path = downloads_dir / filename
    if not file_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(file_path, media_type="video/mp4", filename=filename)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
