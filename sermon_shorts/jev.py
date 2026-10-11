"""Sermon finding with TypeSafe's Jev model, an alternative to the Claude call.

Jev answers typed questions about a piece of text; it doesn't return
timestamps. So the transcript is cut into short windows, Jev labels each one
with the part of the service it comes from, and the sermon is the longest
stretch of "sermon" windows. Short interruptions inside the message (a
prayer, a scripture read aloud, an illustration Jev calls "other") are
bridged rather than allowed to split it.
"""

from __future__ import annotations

import asyncio
import os
import sys

from .highlights import SermonWindow

WINDOW_SECONDS = 45.0   # target length of one labeled window
CONTEXT_CHARS = 600     # neighbouring text shown on each side of a window
MAX_GAP = 120.0         # non-sermon stretch (seconds) still bridged inside the sermon
CONCURRENCY = 8         # parallel Jev requests

LABELS = {
    "worship": "congregational singing, song lyrics, or a worship leader between songs",
    "announcements": "welcome, upcoming events, logistics, or housekeeping",
    "scripture_reading": "a passage of the Bible read aloud with no commentary",
    "sermon": "the main message: the preacher teaching, explaining, illustrating, "
              "or applying scripture",
    "prayer": "someone praying aloud",
    "offering": "giving, tithes, or the offering",
    "communion": "the Lord's Supper liturgy or instructions",
    "other": "anything else: transitions, baptisms, testimonies, the closing or benediction",
}

INSTRUCTIONS = ("This is a speech-recognition transcript of a church service. Which part "
                "of the service is the `window` text from? `before` and `after` are the "
                "words spoken just before and after it, for context only.")


def make_windows(transcript: dict) -> list[dict]:
    """Group whisper segments into windows of about WINDOW_SECONDS."""
    windows: list[dict] = []
    for seg in transcript["segments"]:
        text = seg["text"].strip()
        if not text:
            continue
        if windows and seg["start"] - windows[-1]["start"] < WINDOW_SECONDS:
            windows[-1]["end"] = seg["end"]
            windows[-1]["text"] += " " + text
        else:
            windows.append({"start": seg["start"], "end": seg["end"], "text": text})
    return windows


def _state(windows: list[dict], i: int, total_minutes: int) -> dict:
    before = " ".join(w["text"] for w in windows[max(0, i - 2):i])[-CONTEXT_CHARS:]
    after = " ".join(w["text"] for w in windows[i + 1:i + 3])[:CONTEXT_CHARS]
    return {
        "position": f"minute {int(windows[i]['start'] // 60)} of {total_minutes}",
        "before": before,
        "window": windows[i]["text"],
        "after": after,
    }


async def _label_all(windows: list[dict], api_key: str) -> None:
    from typesafe_sdk import AsyncTypeSafeClient, Choice, TypeSafeAuthenticationError

    question = Choice(instructions=INSTRUCTIONS, criteria=LABELS)
    total_minutes = int(windows[-1]["end"] // 60) + 1
    gate = asyncio.Semaphore(CONCURRENCY)

    async with AsyncTypeSafeClient(api_key=api_key, timeout=60.0) as client:
        async def label(i: int) -> None:
            async with gate:
                r = await client.system_one(state=_state(windows, i, total_minutes),
                                            questions={"part": question})
            answer = r.answers["part"]
            windows[i].update(label=answer.choice, confidence=round(answer.confidence, 3),
                              probabilities={k: round(v, 3)
                                             for k, v in answer.probabilities.items()})

        try:
            await asyncio.gather(*(label(i) for i in range(len(windows))))
        except TypeSafeAuthenticationError as e:
            sys.exit(f"Could not authenticate with Jev ({e}). "
                     "Put JEV_API_KEY=... in the sermon-shorts .env file.")


def sermon_run(windows: list[dict]) -> tuple[int, int] | None:
    """Index range (first, last) of the longest bridged run of sermon windows."""
    runs: list[list[int]] = []
    for i, w in enumerate(windows):
        if w["label"] != "sermon":
            continue
        if runs and w["start"] - windows[runs[-1][1]]["end"] <= MAX_GAP:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    if not runs:
        return None

    def sermon_seconds(run: list[int]) -> float:
        return sum(w["end"] - w["start"] for w in windows[run[0]:run[1] + 1]
                   if w["label"] == "sermon")

    first, last = max(runs, key=sermon_seconds)
    return first, last


def find_sermon_jev(transcript: dict) -> tuple[SermonWindow, list[dict]]:
    """Locate the sermon by labeling transcript windows with Jev.

    Returns the window plus every labeled transcript window, for the record.
    """
    api_key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        sys.exit("--labeler jev needs JEV_API_KEY in the sermon-shorts .env file.")

    windows = make_windows(transcript)
    if not windows:
        sys.exit("No speech found to label.")
    print(f"  labeling {len(windows)} windows of ~{WINDOW_SECONDS:.0f}s with Jev...")

    from typesafe_sdk import TypeSafeError
    try:
        asyncio.run(_label_all(windows, api_key))
    except TypeSafeError as e:
        sys.exit(f"Jev request failed: {e}")

    run = sermon_run(windows)
    if run is None:
        sys.exit("Jev didn't label any part of the service as the sermon.")
    first, last = run
    inside = windows[first:last + 1]
    sermon = [w for w in inside if w["label"] == "sermon"]
    mean_conf = sum(w["confidence"] for w in sermon) / len(sermon)
    window = SermonWindow(
        title="Sermon (labeled by Jev)",
        start=windows[first]["start"],
        end=windows[last]["end"],
        reason=f"longest run of sermon-labeled windows: {len(sermon)} of {len(inside)} "
               f"windows, mean confidence {mean_conf:.2f}",
    )
    return window, windows
