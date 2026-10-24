"""Upload downloaded videos to a private Telegram channel.

Requires two environment variables:
    TELEGRAM_BOT_TOKEN    from @BotFather
    TELEGRAM_CHANNEL_ID   channel identifier, e.g. -1001234567890
"""

import os
from pathlib import Path

from telegram import Bot


def get_telegram_credentials() -> tuple[str, str] | None:
    """Return (bot_token, channel_id) from the environment, or None if missing."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID")
    if not token or not channel_id:
        return None
    return token, channel_id


def make_caption(filename: str) -> str:
    """Build a clean caption from the downloaded filename."""
    name = Path(filename).stem.replace(".", " ").replace("_", " ")
    return name.title()


async def upload_video(video_path: Path, caption: str | None = None) -> dict:
    """Upload a video file to the configured Telegram channel.

    Returns {"ok": True, "message_id": ...} or {"ok": False, "error": ...}.
    """
    creds = get_telegram_credentials()
    if creds is None:
        return {
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID must be set.",
        }

    token, channel_id = creds
    if not video_path.exists():
        return {"ok": False, "error": f"File not found: {video_path}"}

    bot = Bot(token=token)
    text = caption or make_caption(video_path.name)

    try:
        message = await bot.send_video(
            chat_id=channel_id,
            video=video_path,
            caption=text,
            supports_streaming=True,
            read_timeout=300,
            write_timeout=300,
            connect_timeout=60,
        )
        return {"ok": True, "message_id": message.message_id}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
