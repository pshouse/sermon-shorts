"""Command-line entry point: python -m sermon_shorts <video> [options]"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from .transcribe import transcribe, transcript_as_text, snap_to_sentences, words_in_range
from .highlights import select_highlights, find_sermon, Clip, ClipSelection
from .reframe import track_speaker, build_pan_keyframes, crop_filter, video_dimensions
from .captions import write_ass
from .render import render_clip, trim_video
from .thumbnail import pick_thumbnail_frame, render_thumbnail


def _load_church() -> dict | None:
    """Load an optional church profile: church.json in cwd or the project folder."""
    for candidate in (Path.cwd() / "church.json",
                      Path(__file__).resolve().parent.parent / "church.json"):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                sys.exit(f"Invalid JSON in {candidate}: {e}")
    return None


def _slug(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len] or "clip"


def main(argv: list[str] | None = None) -> int:
    # Load ANTHROPIC_API_KEY from a .env file: current directory (or parents)
    # first, then the project folder. Existing env vars are never overridden.
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    parser = argparse.ArgumentParser(
        prog="sermon-shorts",
        description="Turn a full church service recording into vertical, captioned social clips.",
    )
    parser.add_argument("video", type=Path, help="path to the service recording (mp4/mov/mkv)")
    parser.add_argument("--clips", type=int, default=3, help="number of clips to produce (default 3)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: <video name>_clips next to the video)")
    parser.add_argument("--whisper-model", default="small",
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="whisper model size; bigger = more accurate but slower (default small)")
    parser.add_argument("--language", default=None,
                        help="spoken language code, e.g. en, es (default: auto-detect)")
    parser.add_argument("--no-captions", action="store_true", help="skip burned-in captions")
    parser.add_argument("--no-thumbnails", action="store_true",
                        help="skip the designed cover image (<clip>.jpg) generated per clip")
    parser.add_argument("--from-manifest", action="store_true",
                        help="re-render the exact clips in the output folder's clips.json "
                             "instead of asking Claude again (e.g. after fixing a transcript typo)")
    parser.add_argument("--only", type=int, default=None, metavar="N",
                        help="with --from-manifest: re-render only clip number N")
    parser.add_argument("--speaker", default=None, metavar="NAME",
                        help='who is preaching this service, e.g. "Pastor Mike Jones" — '
                             "mentioned in descriptions; overrides church.json for this run")
    parser.add_argument("--sermon-only", action="store_true",
                        help="instead of making clips, trim the service down to just the "
                             "sermon and save it as <video>_sermon.mp4")
    parser.add_argument("--reencode", action="store_true",
                        help="with --sermon-only: frame-accurate cut (slower); default is a "
                             "lossless instant stream copy that cuts on the nearest keyframe")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    video: Path = args.video
    if not video.exists():
        parser.error(f"video not found: {video}")

    out_dir: Path = args.out or video.parent / f"{video.stem}_clips"
    if not args.sermon_only:
        out_dir.mkdir(parents=True, exist_ok=True)

    steps = 3 if args.sermon_only else 4
    print(f"[1/{steps}] Transcribing {video.name}")
    transcript = transcribe(video, model_size=args.whisper_model, language=args.language)
    if not transcript["segments"]:
        sys.exit("No speech found in the video.")

    if args.sermon_only:
        print("[2/3] Finding the sermon with Claude")
        window = find_sermon(transcript_as_text(transcript))
        start, end = snap_to_sentences(transcript, window.start, window.end)
        start = max(0.0, start - 3.0)  # padding also absorbs the keyframe snap
        end = end + 3.0
        minutes = (end - start) / 60
        print(f"  \"{window.title}\" — {start:.0f}s to {end:.0f}s ({minutes:.0f} min)")
        print(f"  ({window.reason})")

        sermon_path = video.parent / f"{video.stem}_sermon.mp4"
        mode = "re-encoding (frame-accurate)" if args.reencode else "stream copy (instant, lossless)"
        print(f"[3/3] Trimming — {mode}")
        trim_video(video, start, end, sermon_path, reencode=args.reencode)
        print(f"Done: {sermon_path}")
        return 0

    manifest_file = out_dir / "clips.json"
    if args.from_manifest:
        if not manifest_file.exists():
            sys.exit(f"--from-manifest: no {manifest_file} found — run without the flag first.")
        print(f"[2/4] Re-rendering clips from {manifest_file.name} (skipping Claude)")
        saved = json.loads(manifest_file.read_text(encoding="utf-8"))
        selection = ClipSelection(
            service_summary=saved.get("summary", ""),
            clips=[Clip(title=c["title"], hook=c.get("hook", ""), start=c["start"],
                        end=c["end"], score=c.get("score", 0), reason=c.get("reason", ""),
                        description=c.get("description", ""))
                   for c in saved["clips"]],
        )
    else:
        church = _load_church()
        if args.speaker:
            church = {**(church or {}), "speaker": args.speaker}
        if church:
            print(f"[2/4] Selecting the {args.clips} best moments with Claude "
                  f"(church profile: {church.get('church_name', 'unnamed')})")
        else:
            print(f"[2/4] Selecting the {args.clips} best moments with Claude")
        selection = select_highlights(transcript_as_text(transcript), args.clips, church=church)
        if not selection.clips:
            sys.exit("Claude did not find any suitable clips. Try a larger --clips value or check the transcript cache.")
        print(f"  service summary: {selection.service_summary}")

    items = list(enumerate(selection.clips, 1))
    if args.only is not None:
        if not args.from_manifest:
            parser.error("--only requires --from-manifest")
        items = [(i, c) for i, c in items if i == args.only]
        if not items:
            sys.exit(f"--only {args.only}: manifest has clips 1-{len(selection.clips)}")

    src_w, src_h = video_dimensions(video)
    manifest = []

    for i, clip in items:
        start, end = snap_to_sentences(transcript, clip.start, clip.end)
        duration = end - start
        print(f"[3/4] Clip {i}/{len(selection.clips)}: \"{clip.title}\" "
              f"({start:.0f}s-{end:.0f}s, {duration:.0f}s, score {clip.score})")

        print("  tracking speaker for vertical crop...")
        times, centers = track_speaker(video, start, end)
        keyframes = build_pan_keyframes(times, centers, duration)
        pans = max(0, (len(keyframes) - 2) // 2)
        if pans:
            print(f"  speaker moves {pans} time(s) — crop will pan to follow")
        vf = crop_filter(src_w, src_h, keyframes)

        out_name = f"{i:02d}_{_slug(clip.title)}.mp4"
        out_path = out_dir / out_name

        with tempfile.TemporaryDirectory() as tmp:
            ass_path = None
            if not args.no_captions:
                words = words_in_range(transcript, start, end)
                if words:
                    ass_path = Path(tmp) / "captions.ass"
                    write_ass(words, clip_start=start, out_path=ass_path)

            print("  rendering...")
            render_clip(video, start, end, vf, ass_path, out_path)

            if not args.no_thumbnails:
                print("  designing cover thumbnail...")
                t_thumb, thumb_center = pick_thumbnail_frame(video, start, end)
                thumb_path = out_path.with_suffix(".jpg")
                render_thumbnail(video, t_thumb, thumb_center, src_w, src_h,
                                 clip.title, thumb_path, Path(tmp))

        print(f"  -> {out_path}")
        if not args.no_thumbnails:
            print(f"  -> {thumb_path.name} (cover image to upload as the thumbnail)")

        if clip.description:
            sidecar = out_path.with_suffix(".txt")
            sidecar.write_text(
                f"{clip.title}\n\n{clip.description}\n", encoding="utf-8"
            )
            print(f"  -> {sidecar.name} (title + description for upload)")

        manifest.append({
            "file": out_name,
            "title": clip.title,
            "hook": clip.hook,
            "start": round(start, 2),
            "end": round(end, 2),
            "score": clip.score,
            "reason": clip.reason,
            "description": clip.description,
        })

    if args.from_manifest and args.only is not None:
        # Partial re-render: replace only the re-rendered entries in the saved manifest
        by_file = {e["file"]: e for e in manifest}
        all_entries = [by_file.get(c["file"], c) for c in saved["clips"]]
    else:
        all_entries = manifest
    manifest_file.write_text(
        json.dumps({"summary": selection.service_summary, "clips": all_entries},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[4/4] Done. {len(manifest)} clip(s) rendered in {out_dir} (details in clips.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
