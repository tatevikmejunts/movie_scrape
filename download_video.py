"""Download the embedded video from a kinogomy.net title page.

Usage:
    python download_video.py                              # S01E01, English audio
    python download_video.py -s 1 -e 1                    # specific episode
    python download_video.py -s 25 -e 1 --lang eng         # force English audio
    python download_video.py -s 1 -e 1 --lang rus          # force Russian audio
    python download_video.py <page_url> -s 1 -e 1 -o out.mp4

The script reads the player's JSON season list, picks the requested episode,
then forces the audio track whose language matches --lang.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = (
    "https://kinogomy.net/detective/"
    "2656-zakon-i-poryadok-specialnyy-korpus-2024.html"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.8",
}

LANGUAGE_SCORES = {
    "eng": ["eng", "english", "original", "оригинал", "англ"],
    "rus": ["rus", "russian", "рус", "русский", "cold film", "ideafilm", "tvshows", "сетс"],
    "ukr": ["ukr", "ukrainian", "укр"],
}

def fetch_text(url: str, referer: str | None = None) -> str:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def find_player_iframe(soup: BeautifulSoup, base_url: str) -> str | None:
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src")
        if src:
            return urljoin(base_url, src)
    return None


def parse_json_array(text: str, key: str) -> list:
    """Extract a JS array assigned to `key:` and return it as a Python list."""
    start = text.find(f"{key}:")
    if start == -1:
        return []
    i = text.find("[", start)
    if i == -1:
        return []

    balance = 0
    in_string = False
    escape = False
    j = i
    while j < len(text):
        char = text[j]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "[":
                balance += 1
            elif char == "]":
                balance -= 1
                if balance == 0:
                    break
        j += 1

    cleaned = re.sub(r",\s*([\]}])", r"\1", text[i:j + 1])
    return json.loads(cleaned)


def extract_streams(seasons: list) -> list[dict]:
    """Flatten seasons/episodes into a list of stream dicts."""
    streams = []
    for season in seasons:
        for episode in season.get("episodes", []):
            audio = episode.get("audio", {}) or {}
            streams.append({
                "season": int(season.get("season", 0)),
                "episode": int(str(episode.get("episode", "0")).split(".")[0]),
                "title": episode.get("title"),
                "hls": episode.get("hls"),
                "audio_names": audio.get("names", []),
                "subtitles": episode.get("cc", []),
            })
    return streams


def language_score(audio_names: list[str], lang: str) -> int:
    """Return how well audio_names match the requested language (higher is better)."""
    needles = LANGUAGE_SCORES.get(lang.lower(), [lang.lower()])
    score = 0
    for name in audio_names:
        lowered = name.lower()
        if any(needle in lowered for needle in needles):
            score += 10
        if "eng.original" in lowered or "оригинал" in lowered:
            score += 5
    return score


def pick_stream(streams: list[dict], season: int, episode: int, lang: str) -> dict | None:
    """Find the best stream for the requested episode and language."""
    candidates = [
        s for s in streams
        if s["season"] == season and s["episode"] == episode and s.get("hls")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda s: language_score(s["audio_names"], lang), reverse=True)
    return candidates[0]


def audio_index_for_language(audio_names: list[str], lang: str) -> int | None:
    """Return the index of the requested language in the player's audio list."""
    needles = LANGUAGE_SCORES.get(lang.lower(), [lang.lower()])
    for index, name in enumerate(audio_names):
        lowered = name.lower()
        if any(needle in lowered for needle in needles):
            return index
    return None


def run_yt_dlp(url: str, out_path: Path) -> bool:
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    template = str(out_path.with_suffix(".%(ext)s"))
    cmd = [
        ytdlp,
        "--no-warnings",
        "-f", "best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ]
    print(f"Trying yt-dlp: {url}")
    return subprocess.run(cmd).returncode == 0


def run_ffmpeg(input_url: str, out_path: Path, duration: int | None = None, map_args: list[str] | None = None) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-headers", f"User-Agent: {HEADERS['User-Agent']}\r\n",
        "-i", input_url,
    ]
    if map_args:
        cmd.extend(map_args)
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.extend(["-c", "copy", "-bsf:a", "aac_adtstoasc", str(out_path)])
    print(f"Trying ffmpeg m3u8 -> {out_path}")
    return subprocess.run(cmd).returncode == 0


def download_subtitles(stream: dict, out_path: Path) -> list[Path]:
    saved: list[Path] = []
    for sub in stream.get("subtitles", []):
        url = sub.get("url")
        name = sub.get("name", "sub")
        if not url:
            continue
        safe_name = re.sub(r"[^\w\-]+", "_", name).strip("_") or "sub"
        sub_path = out_path.with_suffix(f".{safe_name}.vtt")
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"  Failed subtitle {name}: {exc}", file=sys.stderr)
            continue
        sub_path.write_bytes(response.content)
        saved.append(sub_path)
        print(f"  Saved subtitle: {sub_path}")
    return saved


def make_out_path(title: str | None, url: str, out_path: Path | None, season: int, episode: int) -> Path:
    if out_path:
        return out_path
    stem = title or Path(urlparse(url).path).stem or "video"
    stem = re.sub(r"[^\w\-]+", "_", stem).strip("_") or "video"
    return Path(f"{stem}.S{season:02d}E{episode:02d}.mp4")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download video from a kinogomy.net page.")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="page URL")
    parser.add_argument("-s", "--season", type=int, default=1, help="season number (default: %(default)s)")
    parser.add_argument("-e", "--episode", type=int, default=1, help="episode number (default: %(default)s)")
    parser.add_argument("-l", "--lang", default="eng", help="preferred audio language: eng/rus/ukr (default: %(default)s)")
    parser.add_argument("--subs", action="store_true", help="also download subtitle files")
    parser.add_argument("-o", "--output", type=Path, help="output mp4 file")
    parser.add_argument("-t", "--duration", type=int, help="download only the first N seconds (useful for testing audio language)")
    args = parser.parse_args(argv)

    try:
        html = fetch_text(args.url)
    except requests.RequestException as exc:
        print(f"Failed to fetch page: {exc}", file=sys.stderr)
        return 1

    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("h1")
    page_title = title_tag.get_text(strip=True) if title_tag else None

    iframe = find_player_iframe(soup, args.url)
    if not iframe:
        print("No player iframe found on the page.", file=sys.stderr)
        return 1

    print(f"Found player iframe: {iframe}")
    try:
        iframe_html = fetch_text(iframe, referer=args.url)
    except requests.RequestException as exc:
        print(f"Failed to fetch iframe: {exc}", file=sys.stderr)
        return 1

    seasons = parse_json_array(iframe_html, "seasons")
    streams = extract_streams(seasons)
    print(f"Parsed {len(streams)} streams from {len(seasons)} seasons")

    stream = pick_stream(streams, args.season, args.episode, args.lang)
    if not stream:
        print(f"No stream found for S{args.season:02d}E{args.episode:02d}", file=sys.stderr)
        return 1

    print(
        f"Selected S{stream['season']:02d}E{stream['episode']:02d} "
        f"audio={stream['audio_names'] or 'unknown'}"
    )

    out_path = make_out_path(page_title, args.url, args.output, args.season, args.episode)

    if stream.get("subtitles") and args.subs:
        download_subtitles(stream, out_path)

    audio_index = audio_index_for_language(stream["audio_names"], args.lang)
    if audio_index is None:
        print(
            f"Warning: {args.lang.upper()} audio not available for this episode; "
            f"falling back to the player's default audio.",
            file=sys.stderr,
        )
        map_args = None
    else:
        print(f"Forcing audio track index {audio_index} ({args.lang})")
        map_args = ["-map", "0:v:0", "-map", f"0:a:{audio_index}"]

    success = run_ffmpeg(stream["hls"], out_path, duration=args.duration, map_args=map_args)

    if success:
        print(f"Downloaded mp4 to: {out_path}")
        return 0

    # Fallbacks if the structured stream failed.
    if run_yt_dlp(iframe, out_path):
        print(f"Downloaded with yt-dlp: {out_path}")
        return 0

    print("Failed to download video.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
