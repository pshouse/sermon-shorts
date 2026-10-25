"""Command-line entry point: python -m sermon_shorts <video> [options]"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from .transcribe import (transcribe, transcript_as_text, snap_to_sentences,
                         cut_windows, words_in_range, save_shifted_transcript,
                         locate_quote, END_PAD)
from .highlights import select_highlights, find_sermon, Clip, ClipSelection
from .reframe import (FaceDetector, track_speaker, build_pan_keyframes, crop_filter,
                      video_dimensions)
from .captions import write_ass, CAPTION_POSITION_CHOICES, resolve_caption_position
from .render import render_clip, trim_video, quietest_point
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


def _record_sermon_window(video: Path, labeler: str, window, labeled: list | None) -> None:
    """Keep each labeler's raw sermon window side by side, so Claude and Jev
    can be compared on the same service (<video>.sermon-windows.json)."""
    path = video.with_suffix(".sermon-windows.json")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        record = {}
    record[labeler] = window.model_dump()
    if labeled is not None:
        record[labeler]["windows"] = labeled
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def _slug(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len] or "clip"


def main(argv: list[str] | None = None) -> int:
    # Load ANTHROPIC_API_KEY from a .env file: current directory (or parents)
    # first, then the project folder. Existing env vars are never overridden.
    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    # Posting is its own command so it can never ride along with a render run.
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "publish":
        from .publish import main as publish_main
        return publish_main(argv[1:])
    if argv and argv[0] in ("users", "audit"):
        from .accounts_cli import users_main, audit_main
        return (users_main if argv[0] == "users" else audit_main)(argv[1:])
    if argv and argv[0] in ("approve", "reject", "note", "mark-posted", "retry-cover"):
        from .review_cli import main as review_main
        return review_main(argv[0], argv[1:])
    if argv and argv[0] == "serve":
        from .api import serve
        return serve(argv[1:])
    if argv and argv[0] in ("services", "runs"):
        from .records_cli import services_main, runs_main
        return (services_main if argv[0] == "services" else runs_main)(argv[1:])

    # Every render command is recorded as a run (core/runs.py). The run opens
    # once the arguments parse; this wrapper closes it however the pipeline
    # exits. A nested call (--weekly's sermon and clips steps) joins it.
    from .core import runs
    owns = runs.current() is None
    if owns:
        # The web app cancels a run with SIGTERM; turn it into an exit the
        # run can record, instead of dying with the run left open.
        import signal

        def _cancelled(signum, frame):
            raise SystemExit("cancelled")
        try:
            signal.signal(signal.SIGTERM, _cancelled)
        except ValueError:  # not the main thread (e.g. called from a test runner)
            pass
    try:
        rc = _pipeline(argv)
    except SystemExit as e:
        if owns:
            failed = e.code not in (0, None)
            runs.end(error=(str(e.code) if isinstance(e.code, str) else
                            f"exit status {e.code}") if failed else None)
        raise
    except KeyboardInterrupt:
        if owns:
            runs.end(error="interrupted")
        raise
    except BaseException as e:
        if owns:
            runs.end(error=f"{type(e).__name__}: {e}")
        raise
    if owns:
        runs.end(rc)
    return rc


def _record(run, fn, *args, **kwargs):
    """Run one database write for the open run, if there is one."""
    if run is not None:
        from .core.db import write
        with write(run.conn):
            fn(run.conn, *args, **kwargs)


def _pipeline(argv: list[str]) -> int:
    from .core import audit, records, runs

    parser = argparse.ArgumentParser(
        prog="sermon-shorts",
        description="Turn a full church service recording into vertical, captioned social clips.",
    )
    parser.add_argument("video", type=Path, nargs="?", default=None,
                        help="path to the service recording (mp4/mov/mkv); "
                             "or use --latest to fetch it from Subsplash")
    parser.add_argument("--clips", type=int, default=3, help="number of clips to produce (default 3)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: <video name>_clips next to the video)")
    parser.add_argument("--whisper-model", default="small",
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="whisper model size; bigger = more accurate but slower (default small)")
    parser.add_argument("--language", default=None,
                        help="spoken language code, e.g. en, es (default: auto-detect)")
    parser.add_argument("--no-captions", action="store_true", help="skip burned-in captions")
    parser.add_argument("--caption-position", default="auto",
                        choices=CAPTION_POSITION_CHOICES,
                        help="where captions sit in the frame. 'auto' (default) uses the "
                             "tracked face: captions go bottom normally, but lift to the top "
                             "when the speaker sits low in frame (e.g. a zoomed-in camera). "
                             "Force a side with bottom/top/center")
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
    parser.add_argument("--labeler", default="claude", choices=["claude", "jev"],
                        help="with --sermon-only/--weekly: what finds the sermon — Claude "
                             "(default) or TypeSafe's Jev model labeling the transcript "
                             "window by window (needs JEV_API_KEY)")
    parser.add_argument("--reencode", action="store_true",
                        help="with --sermon-only: frame-accurate cut (slower); default is a "
                             "lossless instant stream copy that cuts on the nearest keyframe")
    parser.add_argument("--latest", action="store_true",
                        help="fetch the newest service recording from the church's public "
                             'Subsplash feed instead of giving a file (needs "subsplash" '
                             "in church.json); skips the download if the file is already here")
    parser.add_argument("--download-dir", type=Path, default=Path.home() / "Downloads",
                        metavar="DIR",
                        help="where --latest saves the recording (default: ~/Downloads)")
    parser.add_argument("--weekly", action="store_true",
                        help="the whole Sunday routine in one command: fetch the latest "
                             "service (--latest), trim it to the sermon, then cut clips "
                             "from the sermon; does nothing if that service's clips "
                             "already exist, so it's safe to run on a schedule")
    parser.add_argument("--force", action="store_true",
                        help="with --weekly: redo the service even if its clips already exist")
    from .core.version import label
    parser.add_argument("--version", action="version", version=f"%(prog)s {label()}")
    args = parser.parse_args(argv)

    if runs.current() is None:
        kind = ("weekly" if args.weekly else "sermon" if args.sermon_only
                else "rerender" if args.from_manifest else "clips")
        try:
            runs.begin(kind, argv)
        except runs.RunBusy as e:
            sys.exit(f"Not started: {e}")
    run = runs.current()

    church = _load_church()
    if args.speaker:
        church = {**(church or {}), "speaker": args.speaker}

    if args.weekly:
        args.latest = True

    if args.latest:
        if args.video is not None:
            parser.error("--latest fetches the video for you — drop the file argument")
        subsplash_path = (church or {}).get("subsplash")
        if not subsplash_path:
            parser.error('--latest needs church.json to name your feed, e.g. '
                         '"subsplash": "gastoncommunitychurch" '
                         "(the <name> in subsplash.com/u/<name>)")
        import httpx
        from .subsplash import latest_recording, download, SubsplashError
        try:
            recording = latest_recording(subsplash_path)
            print(f'Latest service on Subsplash: "{recording.title}" ({recording.date}'
                  + (f", {recording.speaker}" if recording.speaker else "") + ")")
            video = download(recording, args.download_dir)
        except (SubsplashError, httpx.HTTPError) as e:
            sys.exit(f"Subsplash fetch failed: {e}")
        service_id = records.service_id_for(video)
        _record(run, records.upsert_service, service_id, source="subsplash",
                title=recording.title, date=recording.date, speaker=recording.speaker,
                recording_file=video)
        run.attach(service_id)
        run.step("fetch", title=recording.title, date=recording.date,
                 speaker=recording.speaker, file=video.name, bytes=recording.file_size)
        if recording.speaker and not args.speaker:
            args.speaker = recording.speaker
            church = {**(church or {}), "speaker": recording.speaker}
    else:
        if args.video is None:
            parser.error("give a video file, or use --latest / --weekly")
        video = args.video
        if not video.exists():
            parser.error(f"video not found: {video}")
        service_id = records.service_id_for(video)
        conn = run.conn
        fields = ({"sermon_file": video.resolve()} if video.stem.endswith("_sermon")
                  else {"recording_file": video.resolve()})
        if records.get_service(conn, service_id) is None:
            fields.update(source="file", date=records.date_in(service_id))
        _record(run, records.upsert_service, service_id, **fields)
        run.attach(service_id)

    if args.weekly:
        sermon = video.parent / f"{video.stem}_sermon.mp4"
        done_manifest = (args.out or sermon.parent / f"{sermon.stem}_clips") / "clips.json"
        # A Subsplash item's video can be swapped after the fact (re-upload,
        # different camera), which changes the filename — so clips for any
        # recording of the same service date also count as done.
        same_day = (sorted(video.parent.glob(f"*{recording.date}*_clips/clips.json"))
                    if recording.date else [])
        already = done_manifest if done_manifest.exists() else (same_day[0] if same_day else None)
        if already and not args.force:
            print(f"Already processed — {already} exists. Use --force to redo.")
            run.skip(f"already processed: {already.parent.name}")
            return 0
        common = ["--whisper-model", args.whisper_model]
        trim_opts = ["--labeler", args.labeler, *(["--reencode"] if args.reencode else [])]
        if args.language:
            common += ["--language", args.language]
        rc = main([str(video), "--sermon-only", *trim_opts, *common])
        if rc:
            return rc
        print(f"\nNow cutting clips from {sermon.name}")
        clip_argv = [str(sermon), "--clips", str(args.clips),
                     "--caption-position", args.caption_position, *common]
        if args.speaker:
            clip_argv += ["--speaker", args.speaker]
        if args.no_captions:
            clip_argv.append("--no-captions")
        if args.no_thumbnails:
            clip_argv.append("--no-thumbnails")
        if args.out:
            clip_argv += ["--out", str(args.out)]
        return main(clip_argv)

    out_dir: Path = args.out or video.parent / f"{video.stem}_clips"
    if not args.sermon_only:
        out_dir.mkdir(parents=True, exist_ok=True)

    steps = 3 if args.sermon_only else 4
    print(f"[1/{steps}] Transcribing {video.name}")
    run.step("transcribe", "start", file=video.name, model=args.whisper_model)
    transcript = transcribe(video, model_size=args.whisper_model, language=args.language)
    if not transcript["segments"]:
        sys.exit("No speech found in the video.")
    run.step("transcribe", segments=len(transcript["segments"]))

    if args.sermon_only:
        if args.labeler == "jev":
            from .jev import find_sermon_jev
            print("[2/3] Finding the sermon with Jev")
            window, labeled = find_sermon_jev(transcript)
        else:
            print("[2/3] Finding the sermon with Claude")
            window = find_sermon(transcript_as_text(transcript))
            labeled = None
        _record_sermon_window(video, args.labeler, window, labeled)
        start, end = snap_to_sentences(transcript, window.start, window.end)
        start = max(0.0, start - 3.0)  # padding also absorbs the keyframe snap
        end = end + 3.0
        minutes = (end - start) / 60
        print(f"  \"{window.title}\" — {start:.0f}s to {end:.0f}s ({minutes:.0f} min)")
        print(f"  ({window.reason})")
        run.step("find_sermon", labeler=args.labeler, title=window.title,
                 start=round(start, 2), end=round(end, 2))

        sermon_path = video.parent / f"{video.stem}_sermon.mp4"
        mode = "re-encoding (frame-accurate)" if args.reencode else "stream copy (instant, lossless)"
        print(f"[3/3] Trimming — {mode}")
        trim_video(video, start, end, sermon_path, reencode=args.reencode)
        run.step("trim", file=sermon_path.name, reencode=args.reencode)

        def _sermon_found(conn):
            records.upsert_service(conn, service_id, sermon_file=sermon_path.resolve(),
                                   sermon_title=window.title, sermon_start=round(start, 2),
                                   sermon_end=round(end, 2), sermon_reason=window.reason,
                                   labeler=args.labeler)
            audit.record(conn, run.actor, "sermon.found", service=service_id,
                         labeler=args.labeler, title=window.title, start=round(start, 2),
                         end=round(end, 2), file=sermon_path.name, run=run.id)
        _record(run, _sermon_found)
        run.outcome = f"sermon trimmed ({minutes:.0f} min)"
        cache = save_shifted_transcript(transcript, sermon_path, args.whisper_model, start, end)
        print(f"  transcript re-based to {cache.name} "
              f"(a later --clips run on the sermon skips transcribing)")
        if not args.reencode:
            print("  note: default stream copy cuts on the nearest keyframe, so "
                  "caption timing may drift a second or two; use --reencode for exact sync")
        print(f"Done: {sermon_path}")
        return 0

    manifest_file = out_dir / "clips.json"
    if args.from_manifest:
        if not manifest_file.exists():
            sys.exit(f"--from-manifest: no {manifest_file} found — run without the flag first.")
        print(f"[2/4] Re-rendering clips from {manifest_file.name} (skipping Claude)")
        report = None
        saved = json.loads(manifest_file.read_text(encoding="utf-8"))
        selection = ClipSelection(
            service_summary=saved.get("summary", ""),
            clips=[Clip(title=c["title"], hook=c.get("hook", ""), ending=c.get("ending", ""),
                        start=c["start"],
                        end=c["end"], score=c.get("score", 0), reason=c.get("reason", ""),
                        description=c.get("description", ""))
                   for c in saved["clips"]],
        )
    else:
        if church:
            print(f"[2/4] Selecting the {args.clips} best moments with Claude "
                  f"(church profile: {church.get('church_name', 'unnamed')})")
        else:
            print(f"[2/4] Selecting the {args.clips} best moments with Claude")
        report: list[dict] = []
        selection = select_highlights(transcript, args.clips, church=church, report=report)
        run.step("select", candidates=len(report), chosen=len(selection.clips))
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
    detector = FaceDetector()
    manifest = []
    rendered_files: dict[int, str] = {}  # id(Clip) -> file, to link candidates to files

    for i, clip in items:
        if args.from_manifest:
            # Saved times are already final cuts; snapping them again would
            # drift the clip a little on every re-render.
            start, end = clip.start, clip.end
        else:
            start, end = snap_to_sentences(transcript, clip.start, clip.end)
            # Cut on the quoted words where they can be found. Snapping to
            # punctuation would reach into the neighbouring thought wherever
            # whisper left out the period between them.
            hook_at = locate_quote(transcript, clip.hook, clip.start)
            end_at = locate_quote(transcript, clip.ending, clip.end, closing=True)
            if hook_at is not None:
                start = hook_at
            if end_at is not None and end_at > start:
                end = end_at + END_PAD
            print(f"  clip {i}: start {'on its opening words' if hook_at is not None else 'snapped (opening words not found)'}, "
                  f"end {'on its closing words' if end_at is not None else 'snapped (closing words not found)'}")
            # Whisper's word timestamps are only good to a few hundred ms, so
            # settle each cut on the quietest instant of the gap around it — and
            # never past the first syllable of the next sentence.
            (s_lo, s_hi), (e_lo, e_hi) = cut_windows(transcript, start, end)
            start = quietest_point(video, s_lo, s_hi, default=start)
            end = quietest_point(video, e_lo, e_hi, default=end)
        duration = end - start
        print(f"[3/4] Clip {i}/{len(selection.clips)}: \"{clip.title}\" "
              f"({start:.0f}s-{end:.0f}s, {duration:.0f}s, score {clip.score})")

        run.step("render", "start", clip=i, title=clip.title)
        print("  tracking speaker for vertical crop...")
        track = track_speaker(video, start, end, detector)
        seen = sum(f is not None for f in track.faces)
        print(f"  speaker seen in {seen}/{len(track.faces)} samples ({detector.name})")
        face_band = track.face_band
        keyframes = build_pan_keyframes(track.times, track.centers, duration)
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
                    position = resolve_caption_position(args.caption_position, face_band)
                    if args.caption_position == "auto":
                        where = f"{face_band[0]:.0%}-{face_band[1]:.0%} of frame" \
                            if face_band else "no face found"
                        print(f"  captions: {position} (auto — face at {where})")
                    ass_path = Path(tmp) / "captions.ass"
                    write_ass(words, clip_start=start, out_path=ass_path,
                              position=position)

            print("  rendering...")
            audio = render_clip(video, start, end, vf, ass_path, out_path)
            print(f"  audio: source {audio['source_lufs']:.1f} LUFS, "
                  f"leveled {audio['gain_db']:+.1f} dB")

            if not args.no_thumbnails:
                print("  designing cover thumbnail...")
                t_thumb, thumb_center = pick_thumbnail_frame(track, start, end)
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

        rendered_files[id(clip)] = out_name
        run.step("render", clip=i, file=out_name, seconds=round(duration, 1))
        manifest.append({
            "file": out_name,
            "title": clip.title,
            "hook": clip.hook,
            "ending": clip.ending,
            "start": round(start, 2),
            "end": round(end, 2),
            "score": clip.score,
            "reason": clip.reason,
            "description": clip.description,
            "fingerprint": records.fingerprint(
                out_path, out_path.with_suffix(".jpg"), clip.title, clip.description),
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

    def _clips_made(conn):
        records.upsert_service(conn, service_id, clips_dir=out_dir.resolve(),
                               summary=selection.service_summary)
        if report is not None:
            for e in report:
                e["clip_file"] = rendered_files.get(id(e.pop("_clip", None)))
            records.replace_candidates(conn, service_id, run.id, report)
        records.record_clips(conn, run.actor, service_id, out_dir, all_entries,
                             {e["file"] for e in manifest}, run.id)
    _record(run, _clips_made)
    run.outcome = f"{len(manifest)} clip(s) rendered"
    run.step("done", clips=len(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
