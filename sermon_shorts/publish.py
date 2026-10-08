"""Post reviewed clips to YouTube Shorts, Instagram Reels and Facebook Reels.

Nothing here runs on its own. The weekly job only renders clips; a clip goes
out when someone who has watched it names it on the command line:

    python -m sermon_shorts publish "<clips folder>" 1 3

Each post is recorded in the folder's published.json, so re-running the same
command never posts a clip to the same platform twice.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

YT_CLIENT_SECRET = PROJECT_DIR / "youtube_client_secret.json"
YT_TOKEN = PROJECT_DIR / ".youtube_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YT_CATEGORY = "29"  # Nonprofits & Activism

GRAPH = "https://graph.facebook.com/v23.0"
PLATFORMS = ("youtube", "instagram", "facebook")
LEDGER = "published.json"
PROCESSING_TIMEOUT = 600  # seconds to wait for Meta to finish processing a video
# Instagram's upload host rejects longer Reels with an opaque ProcessingFailedError,
# whatever the file size. Measured 2026-10-07: 65 s accepted, 70 s rejected.
IG_MAX_SECONDS = 65


class PublishError(Exception):
    pass


# --- published.json ---------------------------------------------------------

def _load_ledger(folder: Path) -> dict:
    path = folder / LEDGER
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _record(folder: Path, ledger: dict, file: str, platform: str, post_id: str, url: str) -> None:
    # Written after every single post, so a failure halfway through a batch
    # still leaves an accurate record of what actually went out.
    ledger.setdefault(file, {})[platform] = {
        "id": post_id, "url": url, "at": datetime.now().isoformat(timespec="seconds"),
    }
    (folder / LEDGER).write_text(json.dumps(ledger, indent=2), encoding="utf-8")


# --- YouTube ----------------------------------------------------------------

def _youtube(interactive: bool = False):
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise PublishError("YouTube support isn't installed: pip install -r requirements.txt")

    creds = None
    if YT_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(YT_TOKEN), YT_SCOPES)
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None  # revoked, or a "Testing" consent screen's 7-day expiry
    if not creds or not creds.valid:
        if not interactive:
            raise PublishError("YouTube isn't connected (or the login expired) — run: "
                               "python -m sermon_shorts publish --connect youtube")
        if not YT_CLIENT_SECRET.exists():
            raise PublishError(f"{YT_CLIENT_SECRET.name} not found in {PROJECT_DIR} — "
                               "see 'Publishing' in the README for how to create it")
        flow = InstalledAppFlow.from_client_secrets_file(str(YT_CLIENT_SECRET), YT_SCOPES)
        creds = flow.run_local_server(port=0)
    YT_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _yt_text(text: str) -> str:
    return text.replace("<", "").replace(">", "")  # YouTube rejects angle brackets


def publish_youtube(clip: dict, mp4: Path, jpg: Path | None) -> tuple[str, str]:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    yt = _youtube()
    body = {
        "snippet": {"title": _yt_text(clip["title"])[:100],
                    "description": _yt_text(clip.get("description", ""))[:5000],
                    "categoryId": YT_CATEGORY},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    try:
        request = yt.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(str(mp4), mimetype="video/mp4",
                                       chunksize=-1, resumable=True))
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=3)
    except HttpError as e:
        raise PublishError(f"YouTube upload failed: {e.reason}") from e

    video_id = response["id"]
    if response.get("status", {}).get("privacyStatus") != "public":
        print("    note: YouTube locked this video as private — until the API project "
              "passes YouTube's audit it can only upload private videos "
              "(see 'Publishing' in the README).")
    if jpg:
        try:
            yt.thumbnails().set(videoId=video_id,
                                media_body=MediaFileUpload(str(jpg), mimetype="image/jpeg")
                                ).execute()
        except HttpError as e:
            print(f"    cover image not set ({e.reason}) — the channel may need phone "
                  "verification for custom thumbnails")
    return video_id, f"https://youtube.com/shorts/{video_id}"


# --- Instagram + Facebook (Meta Graph API) ----------------------------------

def _graph(method: str, path: str, token: str, **kwargs) -> dict:
    url = path if path.startswith("https://") else f"{GRAPH}/{path}"
    try:
        r = httpx.request(method, url, headers={"Authorization": f"Bearer {token}"},
                          timeout=120, **kwargs)
    except httpx.HTTPError as e:
        raise PublishError(f"couldn't reach Meta: {e}") from e
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.is_error or "error" in data:
        err = data.get("error", {})
        raise PublishError(f"Meta API: {err.get('error_user_msg') or err.get('message') or r.text}")
    return data


def _upload_bytes(upload_url: str, token: str, path: Path) -> None:
    """Send a local file to Meta's rupload host (used by both Reels APIs)."""
    data = path.read_bytes()
    try:
        r = httpx.post(upload_url, content=data, timeout=httpx.Timeout(60, write=None),
                       headers={"Authorization": f"OAuth {token}", "offset": "0",
                                "file_size": str(len(data)),
                                "Content-Type": "application/octet-stream"})
    except httpx.HTTPError as e:
        raise PublishError(f"video upload to Meta failed: {e}") from e
    if r.is_error:
        raise PublishError(f"video upload to Meta failed: {r.text}")


def _meta_token() -> str:
    token = os.environ.get("META_PAGE_ACCESS_TOKEN")
    if not token:
        raise PublishError("Facebook/Instagram aren't connected — run: "
                           "python -m sermon_shorts publish --connect meta")
    return token


_meta_ids_cache: dict | None = None


def _meta_ids(token: str) -> dict:
    """The Page and linked Instagram account behind the saved Page token."""
    global _meta_ids_cache
    if _meta_ids_cache is None:
        page = _graph("GET", "me", token, params={"fields": "id,name,instagram_business_account"})
        _meta_ids_cache = {"page_id": page["id"], "page_name": page.get("name", ""),
                           "ig_user_id": page.get("instagram_business_account", {}).get("id")}
    return _meta_ids_cache


def _caption(clip: dict) -> str:
    return f"{clip['title']}\n\n{clip.get('description', '')}".strip()[:2200]


def publish_instagram(clip: dict, mp4: Path, jpg: Path | None) -> tuple[str, str]:
    token = _meta_token()
    ig = _meta_ids(token)["ig_user_id"]
    if not ig:
        raise PublishError("the Facebook Page has no Instagram professional account linked "
                           "(Page settings -> Linked accounts -> Instagram)")
    container = _graph("POST", f"{ig}/media", token, data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": _caption(clip), "share_to_feed": "true",
    })
    _upload_bytes(container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/"
                  f"{GRAPH.rsplit('/', 1)[1]}/{container['id']}", token, mp4)

    deadline = time.monotonic() + PROCESSING_TIMEOUT
    while True:
        state = _graph("GET", container["id"], token, params={"fields": "status_code,status"})
        code = state.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"Instagram couldn't process the video: {state.get('status', code)}")
        if time.monotonic() > deadline:
            raise PublishError("Instagram still processing after 10 min — nothing was posted; "
                               "try again later")
        time.sleep(5)

    media = _graph("POST", f"{ig}/media_publish", token, data={"creation_id": container["id"]})
    link = _graph("GET", media["id"], token, params={"fields": "permalink"}).get("permalink", "")
    # The Graph API only takes a cover from a public URL, so the designed .jpg
    # can't be attached here; Instagram uses the first frame unless changed in the app.
    return media["id"], link


def publish_facebook(clip: dict, mp4: Path, jpg: Path | None) -> tuple[str, str]:
    token = _meta_token()
    page = _meta_ids(token)["page_id"]
    start = _graph("POST", f"{page}/video_reels", token, data={"upload_phase": "start"})
    video_id = start["video_id"]
    _upload_bytes(start["upload_url"], token, mp4)
    _graph("POST", f"{page}/video_reels", token, data={
        "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
        "title": clip["title"], "description": _caption(clip),
    })

    # Publishing finishes asynchronously; wait so processing errors surface here.
    deadline = time.monotonic() + PROCESSING_TIMEOUT
    while time.monotonic() < deadline:
        status = _graph("GET", video_id, token, params={"fields": "status"}).get("status", {})
        if status.get("video_status") == "error":
            raise PublishError(f"Facebook couldn't process the video: {status}")
        if status.get("publishing_phase", {}).get("status") == "complete":
            break
        time.sleep(5)
    else:
        print("    Facebook is still processing — it will go live when done")

    if jpg:
        try:
            _graph("POST", f"{video_id}/thumbnails", token, data={"is_preferred": "true"},
                   files={"source": (jpg.name, jpg.read_bytes(), "image/jpeg")})
        except PublishError as e:
            print(f"    cover image not set ({e})")
    return video_id, f"https://www.facebook.com/reel/{video_id}"


PUBLISHERS = {"youtube": publish_youtube, "instagram": publish_instagram,
              "facebook": publish_facebook}


def _configured() -> list[str]:
    platforms = []
    if YT_TOKEN.exists():
        platforms.append("youtube")
    if os.environ.get("META_PAGE_ACCESS_TOKEN"):
        platforms += ["instagram", "facebook"]
    return platforms


# --- connecting accounts ----------------------------------------------------

def connect_meta() -> None:
    app_id, secret = os.environ.get("META_APP_ID"), os.environ.get("META_APP_SECRET")
    if not app_id or not secret:
        sys.exit("Put META_APP_ID and META_APP_SECRET in .env first "
                 "(see 'Publishing' in the README).")
    user_token = getpass.getpass("Paste the User access token from Graph API Explorer: ").strip()
    # A short-lived user token becomes a long-lived one, and a Page token
    # fetched with a long-lived user token doesn't expire.
    try:
        r = httpx.get(f"{GRAPH}/oauth/access_token", timeout=60, params={
            "grant_type": "fb_exchange_token", "client_id": app_id,
            "client_secret": secret, "fb_exchange_token": user_token})
    except httpx.HTTPError as e:
        sys.exit(f"couldn't reach Meta: {e}")
    if r.is_error:
        sys.exit(f"Token exchange failed: {r.json().get('error', {}).get('message', r.text)}")
    long_token = r.json()["access_token"]

    pages = _graph("GET", "me/accounts", long_token, params={
        "fields": "id,name,access_token,instagram_business_account{username}"})["data"]
    if not pages:
        sys.exit("That login manages no Facebook Pages — was pages_show_list granted?")
    for n, p in enumerate(pages, 1):
        ig = p.get("instagram_business_account", {}).get("username")
        print(f"  {n}. {p['name']}" + (f"  (Instagram @{ig})" if ig else "  (no Instagram linked)"))
    choice = 1 if len(pages) == 1 else int(input("Which Page? ") or 1)
    page = pages[choice - 1]
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), "META_PAGE_ACCESS_TOKEN", page["access_token"])
    print(f"Connected {page['name']} — token saved to {ENV_FILE.name}")
    if not page.get("instagram_business_account"):
        print("  warning: no Instagram account is linked to this Page, "
              "so only Facebook posting will work")


# --- command line -----------------------------------------------------------

def _newest_clips_folder() -> Path | None:
    manifests = sorted((Path.home() / "Downloads").glob("*_clips/clips.json"),
                       key=lambda p: p.stat().st_mtime)
    return manifests[-1].parent if manifests else None


def main(argv: list[str]) -> int:
    load_dotenv()
    load_dotenv(ENV_FILE)

    parser = argparse.ArgumentParser(
        prog="sermon-shorts publish",
        description="Post clips you've reviewed. Name the folder (default: the newest "
                    "*_clips folder in ~/Downloads) and the clip numbers to post.")
    parser.add_argument("targets", nargs="*", metavar="[FOLDER] N",
                        help="optional clips folder, then clip numbers (e.g. 1 3)")
    parser.add_argument("--to", nargs="+", choices=PLATFORMS, default=None,
                        help="platforms to post to (default: every connected platform)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be posted without posting anything")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--connect", choices=["youtube", "meta"],
                        help="one-time login: youtube (opens a browser) or meta "
                             "(Facebook Page + its linked Instagram)")
    args = parser.parse_args(argv)

    if args.connect == "youtube":
        try:
            _youtube(interactive=True)
        except PublishError as e:
            sys.exit(str(e))
        print(f"YouTube connected — login saved to {YT_TOKEN.name}")
        return 0
    if args.connect == "meta":
        connect_meta()
        return 0

    targets = list(args.targets)
    if targets and not targets[0].isdigit():
        folder = Path(targets.pop(0)).expanduser()
    else:
        folder = _newest_clips_folder()
        if folder is None:
            parser.error("no *_clips folder found in ~/Downloads — name the folder")
        print(f"Clips folder: {folder}")
    if not (folder / "clips.json").exists():
        parser.error(f"no clips.json in {folder}")
    clips = json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]
    if not targets:
        for n, c in enumerate(clips, 1):
            print(f"  {n}. {c['title']}  ({c['file']})")
        print("Name the clip number(s) to post, e.g.: publish 1 3")
        return 2
    if not all(t.isdigit() and 1 <= int(t) <= len(clips) for t in targets):
        parser.error(f"clip numbers must be between 1 and {len(clips)}")
    chosen = [clips[int(t) - 1] for t in dict.fromkeys(targets)]

    platforms = args.to or _configured()
    if not platforms:
        sys.exit("No platforms connected yet — run publish --connect youtube and/or "
                 "publish --connect meta (see 'Publishing' in the README).")

    ledger = _load_ledger(folder)
    jobs = []
    for clip in chosen:
        mp4 = folder / clip["file"]
        if not mp4.exists():
            sys.exit(f"{mp4.name} is missing from {folder}")
        jpg = mp4.with_suffix(".jpg")
        for platform in platforms:
            done = ledger.get(clip["file"], {}).get(platform)
            seconds = clip["end"] - clip["start"]
            if done:
                print(f"  already on {platform}: {clip['title']} — {done['url']}")
            elif platform == "instagram" and seconds > IG_MAX_SECONDS:
                print(f"  too long for Instagram's API ({seconds:.0f}s, limit "
                      f"{IG_MAX_SECONDS}s) — post it from the Instagram app: {clip['title']}")
            else:
                jobs.append((clip, mp4, jpg if jpg.exists() else None, platform))
    if not jobs:
        print("Nothing new to post.")
        return 0

    print("\nAbout to post PUBLICLY:")
    for clip, _, _, platform in jobs:
        print(f"  {platform:<9} {clip['title']}")
    if args.dry_run:
        print("(dry run — nothing posted)")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("Not posting without confirmation — re-run with --yes.")
        if input("Post now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1

    failed = 0
    for clip, mp4, jpg, platform in jobs:
        print(f"\n{platform}: {clip['title']}")
        try:
            post_id, url = PUBLISHERS[platform](clip, mp4, jpg)
        except PublishError as e:
            failed += 1
            print(f"    FAILED: {e}")
            continue
        _record(folder, ledger, clip["file"], platform, post_id, url)
        print(f"    posted: {url}")

    print(f"\nDone — {len(jobs) - failed} posted, {failed} failed "
          f"(record in {folder / LEDGER})")
    return 1 if failed else 0
