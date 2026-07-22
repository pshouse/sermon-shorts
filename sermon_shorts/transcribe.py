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


def snap_to_sentences(transcript: dict, start: float, end: float) -> tuple[float, float]:
    """Snap rough clip bounds to whisper segment boundaries so clips don't cut mid-sentence."""
    seg_starts = [s["start"] for s in transcript["segments"]]
    seg_ends = [s["end"] for s in transcript["segments"]]
    if not seg_starts:
        return start, end
    snapped_start = min(seg_starts, key=lambda t: abs(t - start))
    snapped_end = min(seg_ends, key=lambda t: abs(t - end))
    if snapped_end <= snapped_start:
        return start, end
    return snapped_start, snapped_end
