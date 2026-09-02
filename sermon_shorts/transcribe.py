"""Transcription via faster-whisper with word-level timestamps.

faster-whisper decodes the audio track of an MP4/MOV directly (via PyAV),
so no separate audio-extraction step is needed. The transcript is cached
next to the video so re-runs skip the slow step.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _cache_path(video_path: Path, model_size: str) -> Path:
    return video_path.with_suffix(f".transcript-{model_size}.json")


def transcribe(video_path: Path, model_size: str = "small", language: str | None = None) -> dict:
    """Return {"language": str, "segments": [{start, end, text, words: [{start, end, word}]}]}."""
    cache = _cache_path(video_path, model_size)
    if cache.exists():
        print(f"  using cached transcript: {cache.name}")
        return json.loads(cache.read_text(encoding="utf-8"))

    from faster_whisper import WhisperModel

    print(f"  loading whisper model '{model_size}' (downloads on first run)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    print("  transcribing (this is the slow step — roughly 0.1-0.3x realtime on CPU)...")
    segments_iter, info = model.transcribe(
        str(video_path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
    )

    segments = []
    last_report = 0.0
    for seg in segments_iter:
        segments.append({
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": [
                {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
                for w in (seg.words or [])
            ],
        })
        if seg.end - last_report >= 300:  # progress ping every 5 transcribed minutes
            last_report = seg.end
            print(f"    ... transcribed up to {int(seg.end // 60)} min", flush=True)

    result = {"language": info.language, "segments": segments}
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"  transcript cached to {cache.name} ({len(segments)} segments)")
    return result


def shift_transcript(transcript: dict, start: float, end: float) -> dict:
    """Re-base a transcript for a video that was trimmed to [start, end].

    Keeps only the segments/words inside the cut and subtracts `start` from
    every timestamp so t=0 in the result lines up with the first frame of the
    trimmed file. Times are clamped to >= 0 for the (padded) leading segment.
    """
    segments = []
    for seg in transcript["segments"]:
        if seg["end"] < start or seg["start"] > end:
            continue
        words = [
            {"start": max(0.0, round(w["start"] - start, 3)),
             "end": max(0.0, round(w["end"] - start, 3)),
             "word": w["word"]}
            for w in seg["words"]
            if w["end"] >= start and w["start"] <= end
        ]
        segments.append({
            "start": max(0.0, round(seg["start"] - start, 3)),
            "end": max(0.0, round(seg["end"] - start, 3)),
            "text": seg["text"],
            "words": words,
        })
    return {"language": transcript["language"], "segments": segments}


def save_shifted_transcript(transcript: dict, sermon_path: Path, model_size: str,
                            start: float, end: float) -> Path:
    """Write a re-based transcript next to the trimmed sermon so a later
    `--clips` run on that file reuses it instead of transcribing again."""
    shifted = shift_transcript(transcript, start, end)
    cache = _cache_path(sermon_path, model_size)
    cache.write_text(json.dumps(shifted, ensure_ascii=False), encoding="utf-8")
    return cache


def transcript_as_text(transcript: dict) -> str:
    """Compact timestamped text for the highlight-selection prompt."""
    lines = []
    for seg in transcript["segments"]:
        lines.append(f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
    return "\n".join(lines)


def words_in_range(transcript: dict, start: float, end: float) -> list[dict]:
    """All whisper words that fall inside [start, end]."""
    out = []
    for seg in transcript["segments"]:
        if seg["end"] < start or seg["start"] > end:
            continue
        for w in seg["words"]:
            if w["start"] >= start - 0.05 and w["end"] <= end + 0.05:
                out.append(w)
    return out


# Snapping tunables. The end is rounded *forward* to the next real sentence
# end (never backward) so a clip never stops mid-sentence; the small tail pad
# keeps a slightly-early whisper word-end timestamp from clipping the last word
# or the speaker's closing breath.
_SENTENCE_END_CHARS = ".!?…"
_END_PAD = 0.4          # seconds of breathing room added after the final word
_MAX_END_DRIFT = 6.0    # don't extend past this to reach the next sentence end
_MAX_START_DRIFT = 15.0  # don't rewind past this to reach a sentence start
_SNAP_TOLERANCE = 0.5   # slack when deciding a boundary is "at" the requested bound


def _sentence_boundaries(transcript: dict) -> tuple[list[float], list[float]]:
    """Derive true sentence start/end times from word-level punctuation.

    faster-whisper attaches terminal punctuation to the word token it belongs
    to (e.g. "world."), so a sentence ends on any word ending in . ! ? … and
    the next spoken word opens a new one. This is far more reliable than
    segment boundaries, which split on any pause — often mid-sentence.
    """
    starts: list[float] = []
    ends: list[float] = []
    expecting_start = True
    for seg in transcript["segments"]:
        for w in seg.get("words", []):
            text = w["word"].strip()
            if not text:
                continue
            if expecting_start:
                starts.append(w["start"])
                expecting_start = False
            if text[-1] in _SENTENCE_END_CHARS:
                ends.append(w["end"])
                expecting_start = True
    return starts, ends


def snap_to_sentences(transcript: dict, start: float, end: float) -> tuple[float, float]:
    """Snap rough clip bounds to sentence boundaries so clips don't cut mid-sentence.

    Start snaps to the beginning of the sentence at or before the requested
    start; end snaps *forward* to the first sentence end at or after the
    requested end (so the closing thought is never truncated), plus a short
    tail pad. Drift is bounded on both sides: some speakers get almost no
    punctuation from whisper, and an unbounded snap toward a rare sentence
    boundary can silently turn a 60-second clip into a 20-minute one. When no
    sentence boundary is close enough, whisper's segment boundaries (which are
    always dense) are tried, and failing that the requested time is kept.
    """
    seg_starts = [s["start"] for s in transcript["segments"]]
    seg_ends = [s["end"] for s in transcript["segments"]]
    if not seg_starts:
        return start, end

    sent_starts, sent_ends = _sentence_boundaries(transcript)

    # Start: latest boundary not past the requested start, so we open at a
    # sentence (or at least segment) beginning rather than mid-word.
    def _snap_start(pool: list[float]) -> float | None:
        at_or_before = [t for t in pool if t <= start + _SNAP_TOLERANCE]
        if at_or_before and start - max(at_or_before) <= _MAX_START_DRIFT:
            return max(at_or_before)
        return None

    snapped_start = _snap_start(sent_starts)
    if snapped_start is None:
        snapped_start = _snap_start(seg_starts)
    if snapped_start is None:
        snapped_start = start

    # End: earliest boundary at or after the requested end (round up, never
    # truncate the closing thought), within the drift bound.
    def _snap_end(pool: list[float]) -> float | None:
        at_or_after = [t for t in pool if t >= end - _SNAP_TOLERANCE]
        if at_or_after and min(at_or_after) - end <= _MAX_END_DRIFT:
            return min(at_or_after)
        return None

    snapped_end = _snap_end(sent_ends)
    if snapped_end is None:
        snapped_end = _snap_end(seg_ends)
    if snapped_end is None:
        snapped_end = end
    snapped_end += _END_PAD

    if snapped_end <= snapped_start:
        return start, end
    return snapped_start, snapped_end
