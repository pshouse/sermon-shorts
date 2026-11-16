"""Fetch the latest service recording from a church's public Subsplash media feed.

No Subsplash login or API key needed. The public session endpoint
(subsplash.com/u/<name>/api/session) sets a guest token cookie, and
/api/init names the church's app key. That token lists published media
items and resolves the original uploaded MP4 on the Subsplash CDN — the
same file the dashboard's Download button serves.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

# The embed page used to inline the guest JWT. As of the 2026-11-15 weekly
# run it is a script shell with neither the token nor the app key, and the
# web app reads these two instead.
SESSION_URL = "https://subsplash.com/u/{path}/api/session"
INIT_URL = "https://subsplash.com/u/{path}/api/init"
MEDIA_ITEMS_URL = "https://core.subsplash.com/media/v1/media-items"
VIDEO_FILE_URL = "https://core.subsplash.com/files/v1/videos/{id}"

_TIMEOUT = 60
_GUEST_COOKIE = "ss-token-guest"


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


def _guest_token(cookies, subsplash_path: str) -> str | None:
    """The guest JWT for this church.

    The session sets the same cookie twice: once on /u/<path> and once on
    /u/-<app key>. Either is a bearer token for the media API. Prefer the
    one scoped to this church's path.
    """
    wanted = f"/u/{subsplash_path}"
    preferred = fallback = None
    for cookie in cookies:
        if getattr(cookie, "name", None) != _GUEST_COOKIE or not cookie.value:
            continue
        if (cookie.path or "").rstrip("/") == wanted:
            preferred = cookie.value
        elif fallback is None:
            fallback = cookie.value
    return preferred or fallback


def _app_key(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    app = payload.get("appData")
    if not isinstance(app, dict):
        return None
    key = app.get("id")
    return key if isinstance(key, str) and key else None


def _guest_session(subsplash_path: str) -> tuple[httpx.Client, str]:
    """Open a guest session and return a client plus the church's app key.

    /api/session sets ss-token-guest, a JWT good for about a day, and
    /api/init names the app key as appData.id. The media API still takes
    that JWT as a bearer token. weekly #10 (2026-11-15) failed because the
    embed page no longer contains either value.
    """
    client = httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
    try:
        session = client.get(SESSION_URL.format(path=subsplash_path))
        if session.status_code != 200:
            raise SubsplashError(
                f"Subsplash session for '{subsplash_path}' returned "
                f"HTTP {session.status_code} — check the 'subsplash' name in church.json"
            )
        token = _guest_token(client.cookies.jar, subsplash_path)
        if not token:
            raise SubsplashError(
                "Could not find a guest token on the Subsplash session — "
                "the session response may have changed."
            )
        init = client.get(INIT_URL.format(path=subsplash_path), headers={
            "X-Build-Env": "prod", "Accept": "application/json",
        })
        if init.status_code != 200:
            raise SubsplashError(
                f"Subsplash init for '{subsplash_path}' returned HTTP {init.status_code}"
            )
        try:
            app_key = _app_key(init.json())
        except ValueError:
            app_key = None
        if not app_key:
            raise SubsplashError(
                f"Subsplash init for '{subsplash_path}' did not name an app key."
            )
        client.headers["Authorization"] = f"Bearer {token}"
        client.headers["Accept"] = "application/json"
        return client, app_key
    except Exception:
        client.close()
        raise


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
