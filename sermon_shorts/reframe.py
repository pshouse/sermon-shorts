"""Smart 16:9 -> 9:16 reframing with speaker tracking.

Faces are found with OpenCV's YuNet detector (a small bundled ONNX model).
The classic Haar cascade this used to rely on can't see the ~40px face of a
preacher in a wide, dimly lit stage shot — it found a face in 1 of 120
samples on real footage — while YuNet finds it in every sample.

A sample usually holds more than one face: the front row's heads at the
bottom of the frame, faces on the slide behind the stage, the worship leader
sitting to the side. The *speaker* is picked by temporal consistency rather
than size: the detection with the most support across the whole clip anchors
the track, and the rest of the track follows the nearest face sample to
sample, so a preacher walking across the stage stays tracked while a
foreground head that's bigger than the speaker's does not hijack the crop.

The crop holds a fixed position while the speaker stays inside a deadband,
and pans smoothly to the new position when they relocate and stay there. The
motion is compiled into a piecewise-linear ffmpeg crop expression, so
rendering is still a single ffmpeg pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

SAMPLE_STEP = 0.4     # seconds between face samples
DEADBAND = 0.08       # ignore moves smaller than this fraction of frame width
SUSTAIN = 1.2         # seconds a new position must persist before we pan
PAN_TIME = 0.9        # seconds a pan takes

# Detection tunables. Frames are scaled to DETECT_WIDTH before detection —
# YuNet still sees a 30px face at 1280 wide and runs ~2x faster than at 1080p.
DETECT_WIDTH = 1280
MIN_FACE_FRAC = 0.012      # drop "faces" narrower than 1.2% of the frame (slide/screen artefacts)
FOREGROUND_BAND = 0.80     # a face centred below 80% of frame height is probably a front-row head
FOREGROUND_WEIGHT = 0.35   # ...so it is weighted down when choosing the speaker
CLUSTER_RADIUS = (0.06, 0.08)  # (x, y) fraction of frame: "same place" when scoring support
MAX_JUMP = 0.10            # how far (fraction of width) the speaker may move per sample step

MODEL_PATH = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"


@dataclass(frozen=True)
class Face:
    """One detected face, in fractions of the frame (0..1)."""
    x: float       # centre x
    y: float       # centre y
    w: float       # width, as a fraction of frame width
    h: float       # height, as a fraction of frame height
    score: float   # detector confidence 0..1

    @property
    def top(self) -> float:
        return self.y - self.h / 2

    @property
    def bottom(self) -> float:
        return self.y + self.h / 2

    @property
    def prior(self) -> float:
        """How much this looks like the speaker before considering time."""
        weight = FOREGROUND_WEIGHT if self.y > FOREGROUND_BAND else 1.0
        return self.score * weight


@dataclass
class SpeakerTrack:
    """The speaker's position through a clip.

    `times` are relative to the clip start and `centers` are the filtered
    centre-x fractions (same length, gaps interpolated). `faces` holds the
    raw chosen face per sample (None where the speaker wasn't seen) and
    `face_band` is the typical vertical extent (top, bottom) as fractions of
    frame height — None when no face was ever found. Because the crop keeps
    full source height, that fraction carries straight through to the
    output frame, so caption placement can tell which half the face occupies.
    """
    start: float
    times: list[float]
    centers: list[float]
    faces: list[Face | None]
    face_band: tuple[float, float] | None

    def best_sample(self, lo: float = 0.15, hi: float = 0.85) -> tuple[float, float] | None:
        """(absolute time, centre x) of the clearest speaker sighting in the
        middle of the clip — skipping the ends where cuts land — or None."""
        if not self.times or not any(self.faces):
            return None
        span = self.times[-1] if self.times[-1] > 0 else 1.0
        best, best_key = None, -1.0
        for t, f in zip(self.times, self.faces):
            if f is None or not (lo * span <= t <= hi * span):
                continue
            key = f.score * f.w
            if key > best_key:
                best, best_key = (self.start + t, f.x), key
        return best


class FaceDetector:
    """YuNet when available (OpenCV 4.5.4+ and the bundled model), otherwise
    the Haar cascade run at detection resolution rather than 640px."""

    def __init__(self) -> None:
        self._yunet = None
        self._haar = None
        if hasattr(cv2, "FaceDetectorYN") and MODEL_PATH.exists():
            try:
                self._yunet = cv2.FaceDetectorYN.create(
                    str(MODEL_PATH), "", (DETECT_WIDTH, DETECT_WIDTH * 9 // 16),
                    score_threshold=0.4, nms_threshold=0.3, top_k=50)
            except cv2.error:
                self._yunet = None
        if self._yunet is None:
            self._haar = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    @property
    def name(self) -> str:
        return "yunet" if self._yunet is not None else "haar"

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        scale = DETECT_WIDTH / w
        dw, dh = DETECT_WIDTH, max(1, int(round(h * scale)))
        if scale < 1.0:
            small = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA)
        elif scale > 1.0:
            small = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_LINEAR)
        else:
            small = frame

        faces: list[Face] = []
        if self._yunet is not None:
            self._yunet.setInputSize((dw, dh))
            _, dets = self._yunet.detect(small)
            for d in (dets if dets is not None else []):
                x, y, fw, fh = (float(v) for v in d[:4])
                faces.append(Face((x + fw / 2) / dw, (y + fh / 2) / dh,
                                  fw / dw, fh / dh, float(d[14])))
        else:
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            min_px = max(16, int(dw * MIN_FACE_FRAC))
            dets = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                               minSize=(min_px, min_px))
            for x, y, fw, fh in dets:
                faces.append(Face((x + fw / 2) / dw, (y + fh / 2) / dh,
                                  fw / dw, fh / dh, 0.8))
        return [f for f in faces if f.w >= MIN_FACE_FRAC]


def choose_speaker(samples: list[list[Face]]) -> list[Face | None]:
    """Pick the speaker's face in each sample from all faces seen per sample.

    Anchor: the single detection whose neighbourhood (same place, any time)
    collects the most prior-weighted support — the person who is there most
    of the clip, discounted if they sit in the foreground band. Then walk
    outward from the anchor in both directions, each step taking the face
    nearest the last known position (reach grows across unseen samples so
    a brief detection gap doesn't lose a walking speaker).
    """
    n = len(samples)
    rx, ry = CLUSTER_RADIUS
    anchor: Face | None = None
    anchor_i = -1
    anchor_support = -1.0
    for i, faces in enumerate(samples):
        for f in faces:
            support = 0.0
            for others in samples:
                near = [g.prior for g in others
                        if abs(g.x - f.x) <= rx and abs(g.y - f.y) <= ry]
                if near:
                    support += max(near)
            support *= f.prior
            if support > anchor_support:
                anchor, anchor_i, anchor_support = f, i, support

    chosen: list[Face | None] = [None] * n
    if anchor is None:
        return chosen
    chosen[anchor_i] = anchor

    for step in (1, -1):
        last, last_i = anchor, anchor_i
        i = anchor_i + step
        while 0 <= i < n:
            reach = min(MAX_JUMP * abs(i - last_i), 0.5)
            best, best_key = None, -1e9
            for f in samples[i]:
                dx, dy = abs(f.x - last.x), abs(f.y - last.y)
                if dx <= reach and dy <= reach * 1.5 + ry:
                    key = f.prior - 2.0 * (dx + dy)
                    if key > best_key:
                        best, best_key = f, key
            if best is not None:
                chosen[i] = best
                last, last_i = best, i
            i += step
    return chosen


def track_speaker(video_path: Path, start: float, end: float,
                  detector: FaceDetector | None = None) -> SpeakerTrack:
    """Sample the speaker's face position through [start, end]."""
    detector = detector or FaceDetector()
    cap = cv2.VideoCapture(str(video_path))
    times: list[float] = []
    samples: list[list[Face]] = []
    try:
        if cap.isOpened():
            t = start
            while t < end:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
                ok, frame = cap.read()
                faces = detector.detect(frame) if ok and frame is not None else []
                times.append(t - start)
                samples.append(faces)
                t += SAMPLE_STEP
    finally:
        cap.release()

    chosen = choose_speaker(samples) if samples else []
    if not times or not any(chosen):
        return SpeakerTrack(start, [0.0], [0.5], [None], None)

    # Fill detection gaps by interpolating between known positions
    xs = np.array([f.x if f is not None else np.nan for f in chosen], dtype=float)
    idx = np.arange(len(xs))
    known = ~np.isnan(xs)
    xs = np.interp(idx, idx[known], xs[known])

    # Median filter kills single-sample jitter
    if len(xs) >= 5:
        xs = np.array([np.median(xs[max(0, i - 2):i + 3]) for i in range(len(xs))])

    # Median top/bottom gives a single stable band per clip (captions that
    # jitter frame-to-frame would be far more distracting than a fixed choice).
    tops = [f.top for f in chosen if f is not None]
    bottoms = [f.bottom for f in chosen if f is not None]
    face_band = (float(np.median(tops)), float(np.median(bottoms)))

    return SpeakerTrack(start, times, xs.tolist(), chosen, face_band)


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
