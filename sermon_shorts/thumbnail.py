"""Generate a designed 9:16 cover image (thumbnail) for each clip.

Platforms otherwise auto-pick a random mid-clip frame — often one with a
half-finished caption burned in. Instead we pull a *fresh* frame from the
source (so it is caption-free), center the vertical crop on the speaker's
face, and composite the clip's headline in big bold type.

The headline is drawn with an ASS subtitle burned by ffmpeg — the same
mechanism captions.py uses — so this stays cross-platform and needs no extra
Python dependency (Arial Black ships with Windows and macOS).
"""

from __future__ import annotations

from pathlib import Path

import cv2

from .reframe import crop_filter
from .render import ffmpeg_exe, VIDEO_DENOISE, VIDEO_SHARPEN, _run


def pick_thumbnail_frame(video_path: Path, start: float, end: float
                         ) -> tuple[float, float]:
    """Find a good cover frame: the moment with the largest, clearest face.

    Returns (absolute_time, face_center_x_fraction). Samples across the middle
    of the clip (skipping the first/last ~15% where cuts land) and keeps the
    frame whose biggest detected face has the largest area. Falls back to the
    clip midpoint, center-framed, if no face is ever found.
    """
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    cap = cv2.VideoCapture(str(video_path))
    span = end - start
    lo, hi = start + span * 0.15, end - span * 0.15
    step = max(0.4, (hi - lo) / 30.0)  # ~30 samples, never denser than 0.4s

    best_area = 0.0
    best = (start + span / 2.0, 0.5)  # fallback: midpoint, centered
    try:
        if cap.isOpened():
            t = lo
            while t <= hi:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    scale = 640.0 / w if w > 640 else 1.0
                    small = (cv2.resize(frame, (int(w * scale), int(h * scale)))
                             if scale < 1.0 else frame)
                    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                    faces = cascade.detectMultiScale(gray, scaleFactor=1.1,
                                                     minNeighbors=5, minSize=(24, 24))
                    if len(faces) > 0:
                        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                        area = fw * fh
                        if area > best_area:
                            best_area = area
                            best = (t, (x + fw / 2.0) / small.shape[1])
                t += step
    finally:
        cap.release()
    return best


# Big centered headline in the lower third, heavy black outline + soft shadow
# over a translucent scrim so it reads on any frame. libass wraps long titles
# automatically within the L/R margins.
_TITLE_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cover,Arial Black,104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,90,90,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Cover,,0,0,0,,{scrim}
Dialogue: 1,0:00:00.00,0:00:10.00,Cover,,0,0,0,,{text}
"""

# A flat translucent black band across the lower third, drawn as an ASS shape.
_SCRIM = (r"{\an7\pos(0,1230)\1c&H000000&\1a&H70&\bord0\shad0\p1}"
          r"m 0 0 l 1080 0 l 1080 690 l 0 690{\p0}")


def write_title_ass(title: str, out_path: Path) -> None:
    text = title.strip().upper().replace("\n", " ").replace("{", "(").replace("}", ")")
    out_path.write_text(_TITLE_ASS.format(scrim=_SCRIM, text=text), encoding="utf-8")


def render_thumbnail(video_path: Path, time_abs: float, center_x: float,
                     src_w: int, src_h: int, title: str, out_path: Path,
                     workdir: Path) -> None:
    """Extract one frame at time_abs, crop to the face, overlay the headline."""
    ass_path = workdir / "title.ass"
    write_title_ass(title, ass_path)

    # Static crop centered on the face (single keyframe -> constant crop expr).
    crop_scale = crop_filter(src_w, src_h, [(0.0, center_x), (1.0, center_x)])
    vf = f"{VIDEO_DENOISE},{crop_scale},{VIDEO_SHARPEN},subtitles={ass_path.name}"

    cmd = [
        ffmpeg_exe(),
        "-y",
        "-ss", f"{time_abs:.3f}",
        "-i", str(video_path.resolve()),
        "-frames:v", "1",
        "-vf", vf,
        "-q:v", "2",
        str(out_path.resolve()),
    ]
    _run(cmd, out_path, str(workdir))
