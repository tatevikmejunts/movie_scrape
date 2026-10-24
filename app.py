"""FastAPI web UI for downloading videos from kinogomy.net title pages.

Run locally:
    .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://<your-computer-ip>:8000 on your phone (same Wi-Fi).
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import asyncio
from dotenv import load_dotenv

load_dotenv()

import download_video as dv
import telegram_uploader as tu
from bs4 import BeautifulSoup
from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

app = FastAPI(title="Kinogomy Downloader")
# Disable Jinja2 template caching to avoid a cache-key hashability issue
# between certain Starlette/Jinja2 versions on Render.
jinja_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
)
templates = Jinja2Templates(env=jinja_env)

downloads_dir = Path("downloads")
downloads_dir.mkdir(exist_ok=True)

# Limit concurrent downloads so the server stays responsive.
download_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="download")

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


def run_download(
    job_id: str,
    stream: dict,
    out_path: Path,
    lang: str,
    upload_to_tg: bool = False,
    delete_after_upload: bool = False,
) -> None:
    """Background worker that downloads one episode and optionally uploads to Telegram."""
    try:
        download_jobs[job_id]["status"] = "downloading"
        audio_index = dv.audio_index_for_language(stream["audio_names"], lang)
        map_args = None
        if audio_index is not None:
            map_args = ["-map", "0:v:0", "-map", f"0:a:{audio_index}"]
        success = dv.run_ffmpeg(stream["hls"], out_path, map_args=map_args)

        if not success:
            download_jobs[job_id]["status"] = "failed"
            return

        if upload_to_tg and tu.get_telegram_credentials():
            download_jobs[job_id]["status"] = "uploading to Telegram"
            result = asyncio.run(tu.upload_video(out_path))
            if result["ok"]:
                download_jobs[job_id]["status"] = "done + sent to Telegram"
                download_jobs[job_id]["telegram_message_id"] = result["message_id"]
                if delete_after_upload:
                    out_path.unlink(missing_ok=True)
                    download_jobs[job_id]["status"] = "done + sent to Telegram + local file deleted"
            else:
                download_jobs[job_id]["status"] = "done (Telegram upload failed: " + result["error"] + ")"
        else:
            download_jobs[job_id]["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        download_jobs[job_id]["status"] = f"failed: {exc}"


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"default_url": dv.DEFAULT_URL},
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
    upload_to_tg: bool = False,
    delete_after_upload: bool = False,
) -> str:
    """Queue a single episode download and return its job ID."""
    season = stream["season"]
    episode = stream["episode"]
    out_path = dv.make_out_path(page_title, page_url, None, season, episode)
    season_dir = downloads_dir / f"Season {season:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)
    out_path = season_dir / out_path.name

    job_id = f"{season}-{episode}-{lang}-{int(time.time() * 1000)}"
    download_jobs[job_id] = {
        "status": "starting",
        "path": str(out_path),
        "season": season,
        "episode": episode,
        "lang": lang,
        "filename": out_path.name,
    }

    download_executor.submit(run_download, job_id, stream, out_path, lang, upload_to_tg, delete_after_upload)
    return job_id


@app.get("/telegram-configured")
def telegram_configured() -> JSONResponse:
    return JSONResponse({"configured": tu.get_telegram_credentials() is not None})


@app.post("/upload-telegram/{filename}")
def upload_existing_to_telegram(
    filename: str,
    delete_after: bool = Form(False),
) -> JSONResponse:
    file_path = find_downloaded_file(filename)
    if not file_path:
        return JSONResponse({"error": "File not found"}, status_code=404)
    result = asyncio.run(tu.upload_video(file_path))
    if result["ok"] and delete_after:
        file_path.unlink(missing_ok=True)
    return JSONResponse(result)


@app.post("/download")
def start_download(
    page_url: str = Form(...),
    season: int = Form(...),
    episode: int = Form(...),
    lang: str = Form("eng"),
    upload_telegram: bool = Form(False),
    delete_after_upload: bool = Form(False),
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

    job_id = queue_download(
        page_title,
        page_url,
        stream,
        lang,
        upload_to_tg=upload_telegram,
        delete_after_upload=delete_after_upload,
    )
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
    upload_telegram: bool = Form(False),
    delete_after_upload: bool = Form(False),
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
            job_id = queue_download(
                page_title,
                page_url,
                stream,
                lang,
                upload_to_tg=upload_telegram,
                delete_after_upload=delete_after_upload,
            )
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


@app.post("/download-all")
def start_all_downloads(
    page_url: str = Form(...),
    lang: str = Form("eng"),
    upload_telegram: bool = Form(False),
    delete_after_upload: bool = Form(False),
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
    for stream in data["streams"]:
        job_id = queue_download(
            page_title,
            page_url,
            stream,
            lang,
            upload_to_tg=upload_telegram,
            delete_after_upload=delete_after_upload,
        )
        job_ids.append(job_id)

    return JSONResponse({
        "job_ids": job_ids,
        "count": len(job_ids),
        "status": "starting",
    })


class SelectedEpisode(BaseModel):
    season: int
    episode: int


class SelectionPayload(BaseModel):
    page_url: str
    lang: str = "eng"
    upload_telegram: bool = False
    delete_after_upload: bool = False
    episodes: list[SelectedEpisode]


@app.post("/download-selected")
def start_selected_downloads(payload: SelectionPayload) -> JSONResponse:
    try:
        data = fetch_episode_data(payload.page_url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)

    if "error" in data:
        return JSONResponse(data, status_code=400)

    html = dv.fetch_text(payload.page_url)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("h1")
    page_title = title_tag.get_text(strip=True) if title_tag else None

    job_ids = []
    for selected in payload.episodes:
        stream = dv.pick_stream(data["streams"], selected.season, selected.episode, payload.lang)
        if stream:
            job_id = queue_download(
                page_title,
                payload.page_url,
                stream,
                payload.lang,
                upload_to_tg=payload.upload_telegram,
                delete_after_upload=payload.delete_after_upload,
            )
            job_ids.append(job_id)

    return JSONResponse({
        "job_ids": job_ids,
        "count": len(job_ids),
        "status": "starting",
    })


@app.get("/status/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    return JSONResponse(download_jobs.get(job_id, {"status": "not found"}))


@app.post("/status-bulk")
def bulk_status(job_ids: list[str] = Body(...)) -> JSONResponse:
    return JSONResponse({job_id: download_jobs.get(job_id, {"status": "not found"}) for job_id in job_ids})


def find_downloaded_file(filename: str) -> Path | None:
    """Search season subdirectories for a downloaded file."""
    for path in downloads_dir.rglob(filename):
        if path.is_file():
            return path
    return None


@app.get("/files")
def list_files() -> JSONResponse:
    files = []
    for path in sorted(downloads_dir.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        files.append({
            "name": path.name,
            "folder": path.parent.relative_to(downloads_dir).as_posix(),
            "size": path.stat().st_size,
        })
    return JSONResponse({"files": files})


@app.get("/files/{filename}", response_model=None)
def serve_file(filename: str):
    file_path = find_downloaded_file(filename)
    if not file_path:
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(file_path, media_type="video/mp4", filename=filename)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
