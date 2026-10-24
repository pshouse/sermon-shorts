"""Post reviewed clips to YouTube Shorts, Instagram Reels and Facebook Reels.

Nothing here runs on its own. The weekly job only renders clips; a clip goes
out when a named person approves it and someone allowed to publish names it:

    python -m sermon_shorts approve "<clips folder>" 1 3
    python -m sermon_shorts publish "<clips folder>" 1 3

Only clips whose latest decision approves them exactly as they are on disk
are posted (core/review.py). Each post is recorded in the folder's
published.json and the database, so re-running the same command never posts
a clip to the same platform twice.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key

from .core import posts, records, review, users
from .core.audit import Actor
from .core.db import connect
from .core.posts import LEDGER, PLATFORMS

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

YT_CLIENT_SECRET = PROJECT_DIR / "youtube_client_secret.json"
YT_TOKEN = PROJECT_DIR / ".youtube_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YT_CATEGORY = "29"  # Nonprofits & Activism

GRAPH = "https://graph.facebook.com/v23.0"
PROCESSING_TIMEOUT = 600  # seconds to wait for Meta to finish processing a video


class PublishError(Exception):
    pass


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


def set_youtube_cover(video_id: str, jpg: Path, yt=None) -> None:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    yt = yt or _youtube()
    try:
        yt.thumbnails().set(videoId=video_id,
                            media_body=MediaFileUpload(str(jpg), mimetype="image/jpeg")
                            ).execute()
    except HttpError as e:
        raise PublishError(e.reason) from e


def publish_youtube(clip: dict, mp4: Path, jpg: Path | None) -> tuple[str, str, str | None]:
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
    cover = None
    if jpg:
        try:
            set_youtube_cover(video_id, jpg, yt)
            cover = "set"
        except PublishError as e:
            cover = "failed"
            print(f"    cover image not set ({e}) — try again later with retry-cover; "
                  "if it keeps failing, the channel may need phone verification "
                  "for custom thumbnails")
    return video_id, f"https://youtube.com/shorts/{video_id}", cover


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


def publish_instagram(clip: dict, mp4: Path, jpg: Path | None) -> tuple[str, str, None]:
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
    return media["id"], link, None


def publish_facebook(clip: dict, mp4: Path, jpg: Path | None) -> tuple[str, str, str | None]:
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

    cover = None
    if jpg:
        try:
            _graph("POST", f"{video_id}/thumbnails", token, data={"is_preferred": "true"},
                   files={"source": (jpg.name, jpg.read_bytes(), "image/jpeg")})
            cover = "set"
        except PublishError as e:
            cover = "failed"
            print(f"    cover image not set ({e})")
    return video_id, f"https://www.facebook.com/reel/{video_id}", cover


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

USER_ENV = "SERMON_SHORTS_USER"


def main(argv: list[str]) -> int:
    load_dotenv()
    load_dotenv(ENV_FILE)

    parser = argparse.ArgumentParser(
        prog="sermon-shorts publish",
        description="Post approved clips. Name the folder or service (default: the "
                    "newest *_clips folder in ~/Downloads) and the clip numbers to post.")
    parser.add_argument("targets", nargs="*", metavar="[FOLDER] N",
                        help="optional clips folder, then clip numbers (e.g. 1 3)")
    parser.add_argument("--to", nargs="+", choices=PLATFORMS, default=None,
                        help="platforms to post to (default: every connected platform)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be posted without posting anything")
    parser.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--as", dest="as_user", metavar="LOGIN",
                        help=f"who is posting (default: ${USER_ENV}); needs approver or above")
    parser.add_argument("--approve", action="store_true",
                        help="also record your approval of the named clips that aren't "
                             "approved yet (one person reviewing and posting)")
    parser.add_argument("--note", default="", help="with --approve: a note for the record")
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

    conn = connect()
    login = args.as_user or os.environ.get(USER_ENV)
    user = users.get(conn, login) if login else None
    if login and user is None:
        sys.exit(f"No active user {login!r} (see: sermon-shorts users list).")
    if not args.dry_run:
        # Every post is attributed to a person; see 'People and roles' in the README.
        if user is None:
            sys.exit(f"Say who is posting: --as LOGIN, or set {USER_ENV}.")
        if not user.can("publish"):
            sys.exit(f"{user.login} is a {user.role}; publishing needs approver or above.")
        if args.approve and not user.can("approve"):
            sys.exit(f"{user.login} can't approve clips.")
    actor = user.actor("cli") if user else Actor(f"shell:{getpass.getuser()}", "cli")

    targets = list(args.targets)
    try:
        if targets and not targets[0].isdigit():
            folder = records.resolve_folder(conn, targets.pop(0))
        else:
            folder = records.resolve_folder(conn, None)
            print(f"Clips folder: {folder}")
    except FileNotFoundError as e:
        parser.error(str(e))
    clips = records.load_manifest(folder)
    if not targets:
        for n, c in enumerate(clips, 1):
            print(f"  {n}. {c['title']}  ({c['file']})")
        print("Name the clip number(s) to post, e.g.: publish 1 3")
        return 2
    if not all(t.isdigit() and 1 <= int(t) <= len(clips) for t in targets):
        parser.error(f"clip numbers must be between 1 and {len(clips)}")
    chosen = [int(t) for t in dict.fromkeys(targets)]
    for n in chosen:
        if not (folder / clips[n - 1]["file"]).exists():
            sys.exit(f"{clips[n - 1]['file']} is missing from {folder}")

    platforms = args.to or _configured()
    if not platforms:
        sys.exit("No platforms connected yet — run publish --connect youtube and/or "
                 "publish --connect meta (see 'Publishing' in the README).")

    service_id = records.ensure_service(conn, actor, folder)
    jobs, skipped = posts.plan(conn, service_id, folder, chosen, platforms)
    to_approve = []
    if args.approve:
        # One person approving and posting in one step. The approvals are only
        # recorded once the post is confirmed, and are logged as approvals.
        to_approve = [s["n"] for s in skipped if s["kind"] == "not_approved"]
        jobs, skipped = posts.plan(conn, service_id, folder, chosen, platforms,
                                   assume_approved=set(to_approve))
    for sk in skipped:
        title = sk["entry"]["title"]
        if sk["kind"] == "not_approved":
            print(f"  not approved: {title} — {sk['reason']}. Approve it first: "
                  f"sermon-shorts approve {sk['n']} (or publish --approve)")
        elif sk["kind"] == "already":
            print(f"  already on {sk['platform']}: {title} — {sk['reason']}")
        else:
            print(f"  too long for Instagram's API ({sk['reason']}) — post it from the "
                  f"Instagram app, then: sermon-shorts mark-posted {sk['n']} instagram --url URL")
    if not jobs:
        print("Nothing new to post.")
        return 0

    if to_approve:
        names = ", ".join(clips[n - 1]["title"] for n in to_approve)
        print(f"\nWill record your approval ({actor.login}) of: {names}")
    print("\nAbout to post PUBLICLY:")
    for job in jobs:
        print(f"  {job['platform']:<9} {job['entry']['title']}")
    if args.dry_run:
        print("(dry run — nothing posted)")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("Not posting without confirmation — re-run with --yes.")
        if input("Post now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1

    if to_approve:
        for n in to_approve:
            review.decide(conn, user, folder, n, "approve",
                          note=args.note or "approved while publishing (publish --approve)")
        # Re-plan from the recorded decisions: what goes out is what the gate allows.
        jobs, _ = posts.plan(conn, service_id, folder, chosen, platforms)

    ledger = posts.load_ledger(folder)
    failed = 0
    for job in jobs:
        clip, platform = job["entry"], job["platform"]
        mp4 = folder / clip["file"]
        jpg = mp4.with_suffix(".jpg")
        print(f"\n{platform}: {clip['title']}")
        try:
            post_id, url, cover = PUBLISHERS[platform](clip, mp4, jpg if jpg.exists() else None)
        except PublishError as e:
            failed += 1
            print(f"    FAILED: {e}")
            posts.record_failure(conn, actor, service_id, clip["file"], platform, str(e))
            continue
        posts.write_ledger_entry(folder, ledger, clip["file"], platform, {
            "id": post_id, "url": url, "at": datetime.now().isoformat(timespec="seconds"),
            "by": actor.login})
        posts.record_post(conn, actor, service_id, clip["file"], platform, post_id, url,
                          job["fingerprint"], cover=cover)
        print(f"    posted: {url}")

    print(f"\nDone — {len(jobs) - failed} posted, {failed} failed "
          f"(record in {folder / LEDGER})")
    return 1 if failed else 0
