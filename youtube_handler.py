"""YouTube Ingestion Handler for agy2api.

Extracts metadata, transcripts (captions), and downloads ephemeral micro-videos
for visual inspection using yt-dlp and ffmpeg.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("agy2api.youtube")

YOUTUBE_URL_REGEX = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)([\w-]{11})",
    re.IGNORECASE,
)

VISUAL_KEYWORDS = [
    "صف", "مشهد", "بصري", "عين", "شايف", "تراه", "شاشة", "ألوان", "حركة",
    "ملامح", "أشخاص", "شخص", "لابس", "مرئي", "مرئية", "صورة", "لقطة",
    "visual", "look like", "what is shown", "scene", "frame", "appear",
    "wearing", "watch", "see", "sight", "video look", "describe the scene",
]


def extract_youtube_urls(text: str) -> list[str]:
    """Finds all YouTube video URLs in a given text string."""
    if not text:
        return []
    matches = YOUTUBE_URL_REGEX.findall(text)
    urls: list[str] = []
    seen = set()
    for video_id in matches:
        if video_id not in seen:
            seen.add(video_id)
            urls.append(f"https://www.youtube.com/watch?v={video_id}")
    return urls


def is_visual_request(text: str) -> bool:
    """Detects if the user prompt requires visual scene or frame analysis."""
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in VISUAL_KEYWORDS)


def fetch_youtube_metadata_and_transcript(url: str, timeout: int = 30) -> dict:
    """Extracts video title, description, and available subtitles/captions using yt-dlp."""
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--skip-download",
        "--no-playlist",
        "--no-warnings",
        "--extractor-args", "youtube:player_client=android,web",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            logger.warning("yt-dlp metadata failed for %s: %s", url, proc.stderr[:200])
            return {}
        data = json.loads(proc.stdout)
    except Exception as exc:
        logger.warning("Error running yt-dlp for %s: %s", url, exc)
        return {}

    title = data.get("title", "")
    description = data.get("description", "")
    duration = data.get("duration", 0)

    # Try to extract transcript text from subtitles or automatic captions
    transcript_text = ""
    captions = data.get("automatic_captions", {}) or data.get("subtitles", {})
    sub_url = None

    # Priority: Arabic original -> Arabic -> English -> first available
    preferred_langs = ["ar-orig", "ar", "en", "en-orig"]
    for lang in preferred_langs:
        if lang in captions:
            for fmt in captions[lang]:
                if fmt.get("ext") in ("srv3", "json3", "vtt", "ttml"):
                    sub_url = fmt.get("url")
                    break
            if sub_url:
                break

    if not sub_url and captions:
        first_lang = next(iter(captions.values()))
        if first_lang:
            sub_url = first_lang[0].get("url")

    if sub_url:
        try:
            req = urllib.request.Request(sub_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                lines: list[str] = []
                if "<text" in raw:
                    lines = re.findall(r"<text[^>]*>([^<]+)</text>", raw)
                elif raw.strip().startswith("{"):
                    try:
                        js = json.loads(raw)
                        for ev in js.get("events", []):
                            segs = ev.get("segs", [])
                            t = "".join(s.get("utf8", "") for s in segs).strip()
                            if t and t != "\n":
                                lines.append(t)
                    except Exception:
                        pass
                elif "WEBVTT" in raw:
                    for line in raw.splitlines():
                        line = line.strip()
                        if line and "-->" not in line and not line.isdigit() and line != "WEBVTT":
                            lines.append(line)

                if lines:
                    transcript_text = " ".join(lines)
        except Exception as exc:
            logger.debug("Failed fetching transcript URL: %s", exc)

    return {
        "title": title,
        "description": description[:1000] if description else "",
        "duration": duration,
        "transcript": transcript_text.strip(),
    }


def download_micro_video(
    url: str,
    output_dir: Path,
    timeout: int = 35,
) -> Path | None:
    """Downloads an ultra-compressed low-resolution standalone MP4 video clip (144p/240p) for visual inspection."""
    output_dir.mkdir(parents=True, exist_ok=True)
    fname = f"yt_{int(time.time() * 1000)}_{os.urandom(3).hex()}.mp4"
    target_path = output_dir / fname

    # Format 160 is standalone 144p MP4 (no audio merge overhead needed, ~1MB total)
    cmd = [
        "yt-dlp",
        "-f", "160/worstvideo[ext=mp4]/worst[ext=mp4]/worst",
        "--max-filesize", "10M",
        "--no-playlist",
        "--no-warnings",
        "-o", str(target_path),
        url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and target_path.exists() and target_path.stat().st_size > 0:
            logger.info("Downloaded micro video for %s (%d bytes) to %s", url, target_path.stat().st_size, target_path)
            try:
                os.chmod(target_path, 0o644)
            except OSError:
                pass
            return target_path
        else:
            logger.warning("Micro video download failed for %s: %s", url, proc.stderr[:200])
            for leftover in output_dir.glob(f"{target_path.stem}*"):
                leftover.unlink(missing_ok=True)
            return None
    except Exception as exc:
        logger.warning("Timeout or error downloading micro video for %s: %s", url, exc)
        for leftover in output_dir.glob(f"{target_path.stem}*"):
            leftover.unlink(missing_ok=True)
        return None


def process_youtube_request(
    url: str,
    user_context: str,
    media_cache_dir: Path,
) -> Tuple[str, Path | None]:
    """Processes a YouTube video request following the two-tier hybrid strategy:

    Tier 1: Instant title, description, and transcript extraction.
    Tier 2: If visual description requested or transcript missing, downloads ephemeral micro-video.
    """
    wants_visual = is_visual_request(user_context)
    meta = fetch_youtube_metadata_and_transcript(url)

    title = meta.get("title", "")
    desc = meta.get("description", "")
    transcript = meta.get("transcript", "")

    video_file: Path | None = None

    # Tier 2 trigger: User explicitly asks for visual sight/frames OR transcript is empty
    if wants_visual or not transcript:
        logger.info("Triggering micro-video download for %s (wants_visual=%s, has_transcript=%s)", url, wants_visual, bool(transcript))
        video_file = download_micro_video(url, media_cache_dir)

    prompt_parts: list[str] = []

    if video_file:
        prompt_parts.append(
            f"ملف فيديو محلي تم تنزيله بجودة خفيفة للفحص والمشاهدة البصرية:\n{video_file}\n"
            f"عنوان الفيديو: {title}\n"
            f"وصف الفيديو: {desc}\n"
        )
        if transcript:
            prompt_parts.append(f"تفريغ الكلام والنصوص المنطوقة في الفيديو:\n{transcript[:3000]}\n")

        prompt_parts.append(
            "المطلوب بشكل إجباري: اقرأ هذا الملف المرئي باستخدام أداة read_file وافحص لقطاته وإطاراته البصرية بدقة متناهية، "
            "وصِف ما تراه بصرياً كعين لشخص كفيف، وأجب عن طلب المستخدم التالي بدقة:\n"
            f"{user_context}"
        )
    else:
        prompt_parts.append(
            f"معلومات فيديو يوتيوب المستخرجة:\n"
            f"العنوان: {title}\n"
            f"الوصف: {desc}\n"
        )
        if transcript:
            prompt_parts.append(f"نص التفريغ الصوتي وترجمة الكلام المنطوق كاملاً:\n{transcript}\n")
        else:
            prompt_parts.append("[ملاحظة: لم تتوفر ترجمة نصية تلقائية للفيديو]")

        prompt_parts.append(
            "المطلوب: بناءً على تفريغ ومعلومات الفيديو أعلاه، أجب عن طلب وسؤال المستخدم التالي بدقة وبشكل شامل:\n"
            f"{user_context}"
        )

    return "\n\n".join(prompt_parts), video_file
