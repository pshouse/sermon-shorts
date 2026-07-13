"""Smart 16:9 -> 9:16 reframing with speaker tracking.

The speaker's face is sampled every ~0.4s across the clip. The crop holds a
fixed position while the speaker stays inside a deadband, and pans smoothly
to the new position when they relocate and stay there — so a preacher walking
across the stage is followed, but small sways and gestures don't jiggle the
frame. The motion is compiled into a piecewise-linear ffmpeg crop expression,
so rendering is still a single ffmpeg pass.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

SAMPLE_STEP = 0.4     # seconds between face samples
DEADBAND = 0.08       # ignore moves smaller than this fraction of frame width
SUSTAIN = 1.2         # seconds a new position must persist before we pan
PAN_TIME = 0.9        # seconds a pan takes


def track_speaker(video_path: Path, start: float, end: float) -> tuple[list[float], list[float]]:
    """Sample the speaker's horizontal position through [start, end].

    Returns (times relative to clip start, center-x fractions 0..1).
    Gaps where no face was found are interpolated; if no face is ever
    found the track is a constant 0.5 (center crop).
    """
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    cap = cv2.VideoCapture(str(video_path))
    times: list[float] = []
    raw: list[float | None] = []
    try:
        if cap.isOpened():
            t = start
            while t < end:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                center = None
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
                        center = (x + fw / 2.0) / small.shape[1]
                times.append(t - start)
                raw.append(center)
                t += SAMPLE_STEP
    finally:
        cap.release()

    if not times or all(c is None for c in raw):
        return [0.0], [0.5]

    # Fill detection gaps by interpolating between known positions
    xs = np.array([c if c is not None else np.nan for c in raw], dtype=float)
    idx = np.arange(len(xs))
    known = ~np.isnan(xs)
    xs = np.interp(idx, idx[known], xs[known])

    # Median filter kills single-frame misdetections (e.g. a face in the crowd)
    if len(xs) >= 5:
        xs = np.array([np.median(xs[max(0, i - 2):i + 3]) for i in range(len(xs))])

    return times, xs.tolist()


def build_pan_keyframes(times: list[float], centers: list[float],
                        duration: float) -> list[tuple[float, float]]:
    """Reduce the track to hold-and-pan keyframes: [(t, center_x_frac), ...]."""
    hold = centers[0]
    keyframes: list[tuple[float, float]] = [(0.0, hold)]
    i = 0
    n = len(times)
    while i < n:
        if abs(centers[i] - hold) > DEADBAND:
            # Only pan if the new position persists for SUSTAIN seconds
            t_limit = times[i] + SUSTAIN
            window = [c for t, c in zip(times[i:], centers[i:]) if t <= t_limit]
            if len(window) >= 2 and all(abs(c - hold) > DEADBAND * 0.6 for c in window):
                target = float(np.median(window))
                pan_start = max(times[i] - 0.2, keyframes[-1][0] + 0.05)
                keyframes.append((pan_start, hold))
                keyframes.append((pan_start + PAN_TIME, target))
                hold = target
                while i < n and times[i] < pan_start + PAN_TIME:
                    i += 1
                continue
        i += 1
    keyframes.append((duration, hold))
    return keyframes


def crop_filter(src_w: int, src_h: int, keyframes: list[tuple[float, float]]) -> str:
    """Build an ffmpeg crop+scale filter from pan keyframes (static if none)."""
    crop_w = int(src_h * 9 / 16) & ~1
    crop_w = min(crop_w, src_w)

    def px(frac: float) -> int:
        x = int(round(frac * src_w - crop_w / 2.0))
        return max(0, min(x, src_w - crop_w))

    xs = [px(f) for _, f in keyframes]
    if len(set(xs)) == 1:
        return f"crop={crop_w}:{src_h}:{xs[0]}:0,scale=1080:1920:flags=lanczos"

    kfs = [(t, x) for (t, _), x in zip(keyframes, xs)]
    expr = _piecewise_expr(kfs)
    # Quotes protect the commas inside if(...) from the filtergraph parser
    return f"crop={crop_w}:{src_h}:'{expr}':0,scale=1080:1920:flags=lanczos"


def _piecewise_expr(kfs: list[tuple[float, int]]) -> str:
    """Piecewise-linear x(t) through keyframes as a nested ffmpeg expression."""
    if len(kfs) == 1:
        return str(kfs[0][1])
    (t0, x0), (t1, x1) = kfs[0], kfs[1]
    if x0 == x1 or t1 - t0 < 0.01:
        seg = str(x0)
    else:
        seg = f"{x0}+({x1 - x0})*(t-{t0:.2f})/{t1 - t0:.2f}"
    return f"if(lt(t,{t1:.2f}),{seg},{_piecewise_expr(kfs[1:])})"


def video_dimensions(video_path: Path) -> tuple[int, int]:
    cap = cv2.VideoCapture(str(video_path))
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    if w <= 0 or h <= 0:
        raise RuntimeError(f"Could not read video dimensions from {video_path}")
    return w, h
