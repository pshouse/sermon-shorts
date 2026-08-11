"""Fetch the latest service recording from a church's public Subsplash media feed.

No Subsplash login or API key needed: the public media embed page
(subsplash.com/u/<name>/media/embed) hands every visitor a guest token, and
that token is enough to list published media items and resolve the original
uploaded MP4 on the Subsplash CDN — the same file the dashboard's Download
button serves.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

EMBED_URL = "https://subsplash.com/u/{path}/media/embed/d/*recent"
MEDIA_ITEMS_URL = "https://core.subsplash.com/media/v1/media-items"
VIDEO_FILE_URL = "https://core.subsplash.com/files/v1/videos/{id}"

_TIMEOUT = 60


@dataclass
class ServiceRecording:
    title: str          # media item title, e.g. "The New Covenant"
    date: str           # YYYY-MM-DD
    speaker: str | None
    filename: str       # original upload name, e.g. "Live_2026-08-02_7am.mp4"
    mp4_url: str        # direct CDN URL of the full-quality MP4
    file_size: int | None


class SubsplashError(RuntimeError):
    pass


def _guest_session(subsplash_path: str) -> tuple[httpx.Client, str]:
    """Load the public embed page and return a client + the app key it names.

    The embed page inlines a guest JWT (valid for a few hours) and the
    church's app key; both are needed for the media API.
    """
    client = httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
    page = client.get(EMBED_URL.format(path=subsplash_path))
    if page.status_code != 200:
        raise SubsplashError(
            f"Subsplash embed page for '{subsplash_path}' returned "
            f"HTTP {page.status_code} — check the 'subsplash' name in church.json"
        )
    token = re.search(r'\\"guest\\":\\"(eyJ[\w.-]+)\\"', page.text)
    app_key = re.search(r'\\"app_key\\":\\"([A-Z0-9]+)\\"', page.text)
    if not token or not app_key:
        raise SubsplashError(
            "Could not find a guest token on the Subsplash embed page — "
            "the page layout may have changed."
        )
    client.headers["Authorization"] = f"Bearer {token.group(1)}"
    client.headers["Accept"] = "application/json"
    return client, app_key.group(1)


def latest_recording(subsplash_path: str) -> ServiceRecording:
    """Return the newest published media item that has a video attached."""
    client, app_key = _guest_session(subsplash_path)
    try:
        items = client.get(MEDIA_ITEMS_URL, params={
            "filter[app_key]": app_key,
            "sort": "-date",
            "page[size]": 5,
        }).raise_for_status().json().get("_embedded", {}).get("media-items", [])

        for item in items:
            video_ref = item.get("_embedded", {}).get("video")
            if not video_ref:
                continue  # audio-only or not yet processed
            video = client.get(
                VIDEO_FILE_URL.format(id=video_ref["id"])
            ).raise_for_status().json()
            outputs = video.get("_embedded", {}).get("video-outputs", [])
            mp4s = [o for o in outputs
                    if o.get("content_type") == "video/mp4"
                    and o.get("_links", {}).get("related", {}).get("href")]
            if not mp4s:
                continue
            best = max(mp4s, key=lambda o: o.get("file_size") or 0)
            return ServiceRecording(
                title=item.get("title") or "Untitled service",
                date=(item.get("date") or "")[:10],
                speaker=item.get("speaker") or None,
                filename=video.get("title") or f"{item.get('slug', 'service')}.mp4",
                mp4_url=best["_links"]["related"]["href"],
                file_size=best.get("file_size"),
            )
    finally:
        client.close()
    raise SubsplashError("No published media item with a downloadable video found.")


def download(recording: ServiceRecording, dest_dir: Path) -> Path:
    """Download the recording's MP4 into dest_dir, skipping if already there.

    A file that exists with the expected size is reused. Downloads go to a
    .part file first, so an interrupted run never leaves a half file that a
    later run would mistake for complete.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / recording.filename
    if dest.exists() and recording.file_size and dest.stat().st_size == recording.file_size:
        print(f"  already downloaded: {dest} ({dest.stat().st_size / 1e6:.0f} MB) — skipping")
        return dest

    part = dest.with_suffix(dest.suffix + ".part")
    size_mb = f"{recording.file_size / 1e6:.0f} MB" if recording.file_size else "size unknown"
    print(f"  downloading {recording.filename} ({size_mb})")
    done = 0
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        with client.stream("GET", recording.mp4_url) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0)) or recording.file_size
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total and sys.stdout.isatty():
                        print(f"\r  {done / 1e6:.0f}/{total / 1e6:.0f} MB "
                              f"({done * 100 // total}%)", end="", flush=True)
    if sys.stdout.isatty():
        print()
    if recording.file_size and done != recording.file_size:
        part.unlink(missing_ok=True)
        raise SubsplashError(
            f"Download came back {done} bytes but Subsplash reports "
            f"{recording.file_size} — network hiccup? Try again."
        )
    part.replace(dest)
    print(f"  -> {dest}")
    return dest
