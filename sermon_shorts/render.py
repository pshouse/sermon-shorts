"""Render clips with the ffmpeg binary bundled by imageio-ffmpeg.

The ASS file is referenced by bare filename with ffmpeg running in the same
directory — this sidesteps the subtitles-filter path-escaping mess on
Windows (drive-letter colons) entirely.

This module also owns the small audio-analysis helpers that need ffmpeg:
loudness measurement for the two-pass speech chain and finding a quiet
point to cut on.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
import numpy as np


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


# --- Speech audio chain -----------------------------------------------------
#
# Church recordings are quiet (often -30 LUFS or below) and *peaky*: a room
# or podium mic gives 20+ dB between the loudest syllable and the body of the
# voice. A plain normalizer is then limited by the peaks and the speech still
# sounds thin. So the chain is:
#
#   highpass    cut sub-80 Hz rumble / handling noise
#   volume      pre-level the clip to PRE_LEVEL_LUFS (measured), so the
#               compressor below sees every clip at the same level
#   acompressor 4:1 above -26 dB tames the peaks so the body can come up
#   volume      measured make-up gain that lands on TARGET_LUFS
#   alimiter    brickwall at TRUE_PEAK_DB so nothing clips (level=false, or
#               the limiter re-normalizes and undoes the gain staging)
#   afade       short fade in/out so cuts never click or catch a syllable
#
# Two measurement passes (audio only, sub-second each) make the final level
# deterministic — ffmpeg's single-pass loudnorm was landing 2+ LU short on
# dynamic clips.
AUDIO_CLEAN = "highpass=f=80"
PRE_LEVEL_LUFS = -23.0
COMPRESSOR = "acompressor=threshold=-26dB:ratio=4:attack=3:release=120:knee=5"
TARGET_LUFS = -13.0     # a touch above the -14 platform target: Reels/TikTok
                        # don't normalize down much, YouTube will if it must
TRUE_PEAK_DB = -1.0
FADE_IN = 0.05
FADE_OUT = 0.12

# Video cleanup, applied around the upscale. Source recordings are usually
# low-bitrate 720p that gets cropped and scaled 2-3x, so we lightly denoise
# the compression mush *before* upscaling, then add a gentle sharpen after so
# edges don't go soft. This can't add detail that was never recorded — the
# real ceiling is the source bitrate/resolution — but it cleans up the look.
VIDEO_DENOISE = "hqdn3d=2:1.5:6:6"
VIDEO_SHARPEN = "unsharp=5:5:0.8:5:5:0.0"


def measure_loudness(video_path: Path, start: float, end: float, af: str) -> float:
    """Integrated loudness (LUFS) of [start, end] after running it through `af`."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video_path.resolve()),
           "-vn", "-af", f"{af},loudnorm=print_format=json", "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    text = result.stderr
    lo, hi = text.rfind("{"), text.rfind("}")
    if result.returncode != 0 or lo < 0 or hi < lo:
        raise RuntimeError("ffmpeg loudness measurement failed:\n"
                           + "\n".join(text.splitlines()[-10:]))
    value = float(json.loads(text[lo:hi + 1])["input_i"])
    return max(value, -70.0)  # silence reports -inf


def speech_audio_filter(video_path: Path, start: float, end: float) -> tuple[str, dict]:
    """Build the speech chain for this clip. Returns (filter, info for logging)."""
    source = measure_loudness(video_path, start, end, AUDIO_CLEAN)
    pre_gain = PRE_LEVEL_LUFS - source
    staged = f"{AUDIO_CLEAN},volume={pre_gain:.2f}dB,{COMPRESSOR}"
    compressed = measure_loudness(video_path, start, end, staged)
    make_up = TARGET_LUFS - compressed
    limit = 10 ** (TRUE_PEAK_DB / 20)
    fade_out_at = max(0.0, (end - start) - FADE_OUT)
    af = (f"{staged},volume={make_up:.2f}dB,"
          f"alimiter=limit={limit:.3f}:attack=5:release=50:level=false,"
          f"afade=t=in:st=0:d={FADE_IN},afade=t=out:st={fade_out_at:.3f}:d={FADE_OUT},"
          f"aresample=48000")
    return af, {"source_lufs": source, "gain_db": pre_gain + make_up}


def render_clip(
    video_path: Path,
    start: float,
    end: float,
    vf_crop_scale: str,
    ass_path: Path | None,
    out_path: Path,
) -> dict:
    """Render one clip; returns the audio info dict from speech_audio_filter."""
    # denoise (full frame) -> crop+scale -> sharpen -> burn captions last, so
    # the captions are rendered crisp at 1080x1920 and never sharpened/denoised.
    vf = f"{VIDEO_DENOISE},{vf_crop_scale},{VIDEO_SHARPEN}"
    workdir = str(ass_path.parent) if ass_path else str(out_path.parent)
    if ass_path:
        vf += f",subtitles={ass_path.name}"

    af, info = speech_audio_filter(video_path, start, end)

    cmd = [
        ffmpeg_exe(),
        "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", str(video_path.resolve()),
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ]

    _run(cmd, out_path, workdir)
    return info


def quietest_point(video_path: Path, lo: float, hi: float, default: float,
                   window: float = 0.02) -> float:
    """The centre of the quietest `window` seconds inside [lo, hi].

    Used to move a clip boundary off the transcript's word timestamp (which
    can be a few hundred ms out) and onto the actual gap between words.
    Returns `default` if the range is degenerate or the audio can't be read.
    """
    if hi - lo < window * 2:
        return default
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "error",
           "-ss", f"{lo:.3f}", "-to", f"{hi:.3f}", "-i", str(video_path.resolve()),
           "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or len(result.stdout) < 2 * 16000 * window * 2:
        return default
    pcm = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float64)
    n = int(16000 * window)
    frames = len(pcm) // n
    if frames < 2:
        return default
    energy = (pcm[:frames * n].reshape(frames, n) ** 2).mean(axis=1)
    i = int(np.argmin(energy))
    return lo + (i + 0.5) * window


def trim_video(video_path: Path, start: float, end: float, out_path: Path,
               reencode: bool = False) -> None:
    """Cut [start, end] out of the source at original resolution.

    Default is a stream copy: no quality loss and near-instant, but the cut
    lands on the nearest keyframe (usually within a few seconds — absorbed by
    padding). Pass reencode=True for frame-accurate cuts at the cost of a
    full re-encode.
    """
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-ss", f"{start:.3f}",
        "-i", str(video_path.resolve()),
        "-t", f"{end - start:.3f}",
    ]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(out_path.resolve())]
    _run(cmd, out_path, str(out_path.parent))


def _run(cmd: list[str], out_path: Path, workdir: str) -> None:
    result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-15:])
        raise RuntimeError(f"ffmpeg failed for {out_path.name}:\n{tail}")
