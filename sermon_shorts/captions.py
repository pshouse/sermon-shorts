"""Generate burned-in captions as an ASS subtitle file.

Words from the whisper transcript are grouped into short chunks (max 3 words
or ~1.2s) that pop on screen in sequence — the standard short-form style.
Uses Arial Black, which ships with both Windows and macOS, keeping the tool
self-contained.
"""

from __future__ import annotations

from pathlib import Path

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,Arial Black,88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _chunk_words(words: list[dict], max_words: int = 3, max_span: float = 1.4) -> list[dict]:
    chunks = []
    current: list[dict] = []
    for w in words:
        if current and (
            len(current) >= max_words
            or w["end"] - current[0]["start"] > max_span
            or w["start"] - current[-1]["end"] > 0.8  # pause -> new chunk
        ):
            chunks.append(current)
            current = []
        current.append(w)
    if current:
        chunks.append(current)

    out = []
    for chunk in chunks:
        text = "".join(w["word"] for w in chunk).strip()
        if text:
            out.append({"start": chunk[0]["start"], "end": chunk[-1]["end"], "text": text})
    return out


def write_ass(words: list[dict], clip_start: float, out_path: Path) -> None:
    """Write captions timed relative to the start of the clip."""
    lines = [ASS_HEADER]
    for chunk in _chunk_words(words):
        start = chunk["start"] - clip_start
        end = chunk["end"] - clip_start + 0.08  # tiny hold so chunks don't flicker
        text = chunk["text"].replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Pop,,0,0,0,,{text}\n"
        )
    out_path.write_text("".join(lines), encoding="utf-8")
