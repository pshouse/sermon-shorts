"""Render clips with the ffmpeg binary bundled by imageio-ffmpeg.

The ASS file is referenced by bare filename with ffmpeg running in the same
directory — this sidesteps the subtitles-filter path-escaping mess on
Windows (drive-letter colons) entirely.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def render_clip(
    video_path: Path,
    start: float,
    end: float,
    vf_crop_scale: str,
    ass_path: Path | None,
    out_path: Path,
) -> None:
    vf = vf_crop_scale
    workdir = str(ass_path.parent) if ass_path else str(out_path.parent)
    if ass_path:
        vf += f",subtitles={ass_path.name}"

    cmd = [
        ffmpeg_exe(),
        "-y",
        "-ss", f"{start:.3f}",
        "-to", f"{end:.3f}",
        "-i", str(video_path.resolve()),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ]

    _run(cmd, out_path, workdir)


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
