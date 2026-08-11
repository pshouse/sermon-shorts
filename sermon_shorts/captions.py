"""Generate burned-in captions as an ASS subtitle file.

Words from the whisper transcript are grouped into short chunks (max 3 words
or ~1.2s) that pop on screen in sequence — the standard short-form style.
Uses Arial Black, which ships with both Windows and macOS, keeping the tool
self-contained.
"""

from __future__ import annotations

from pathlib import Path

# Where the caption block sits in the 1080x1920 frame, as (ASS alignment,
# vertical margin). Alignment is the numpad anchor: 2 = bottom-center,
# 8 = top-center, 5 = middle. MarginV is measured from the anchored edge, so
# for "top" it is the gap below the top edge and for "bottom" the gap above the
# bottom edge. "top" keeps captions in the upper half — useful when the camera
# is zoomed so the speaker's face sits low in frame and bottom captions would
# cover it.
CAPTION_POSITIONS: dict[str, tuple[int, int]] = {
    "bottom": (2, 420),
    "top": (8, 220),
    "center": (5, 0),
}
DEFAULT_CAPTION_POSITION = "bottom"

# "auto" is a CLI choice resolved per clip by resolve_caption_position() — it is
# not a real render position, so it's kept out of CAPTION_POSITIONS.
CAPTION_POSITION_CHOICES = ["auto", *CAPTION_POSITIONS]

# The vertical band (fraction of frame height) the caption block roughly covers
# in each fixed position. Used only to decide, in "auto" mode, whether the
# tracked face would collide with a caption there. Mirrors the margins above:
# bottom captions sit low, top captions sit high.
_CAPTION_BANDS = {
    "bottom": (0.66, 0.92),
    "top": (0.10, 0.30),
}


def resolve_caption_position(position: str, face_band: tuple[float, float] | None) -> str:
    """Turn "auto" into a concrete position from the speaker's face position.

    `face_band` is the face's (top, bottom) as fractions of frame height, or
    None when no face was tracked. Prefers "bottom" — the readable, expected
    place — and only lifts captions to the top when the face sits low enough to
    collide with a bottom caption. If the face fills the frame (collides with
    both), it keeps captions on the side with more clearance. Any non-"auto"
    value is returned unchanged.
    """
    if position != "auto":
        return position
    if face_band is None:
        return "bottom"
    face_top, face_bottom = face_band
    if face_bottom <= _CAPTION_BANDS["bottom"][0]:
        return "bottom"                       # face clears the bottom caption
    if face_top >= _CAPTION_BANDS["top"][1]:
        return "top"                          # face is low, top is clear
    # Face spans both bands — keep captions where there's more room.
    return "top" if face_top > (1.0 - face_bottom) else "bottom"


def _ass_header(position: str) -> str:
    alignment, margin_v = CAPTION_POSITIONS[position]
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,Arial Black,88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,{alignment},60,60,{margin_v},1

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


def write_ass(words: list[dict], clip_start: float, out_path: Path,
              position: str = DEFAULT_CAPTION_POSITION) -> None:
    """Write captions timed relative to the start of the clip.

    `position` is one of CAPTION_POSITIONS ("bottom", "top", "center") and sets
    where the caption block sits vertically in the frame.
    """
    lines = [_ass_header(position)]
    for chunk in _chunk_words(words):
        start = chunk["start"] - clip_start
        end = chunk["end"] - clip_start + 0.08  # tiny hold so chunks don't flicker
        text = chunk["text"].replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Pop,,0,0,0,,{text}\n"
        )
    out_path.write_text("".join(lines), encoding="utf-8")
