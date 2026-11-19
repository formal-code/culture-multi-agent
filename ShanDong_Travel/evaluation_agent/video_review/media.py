"""Read real media with FFmpeg. Never alter the source or remove audio pauses."""
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ffmpeg(arguments, timeout=180):
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-nostdin", *map(str, arguments)],
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # Keep paths/arguments and large stderr out of user-facing exceptions.
        raise ValueError("FFmpeg 媒体处理失败，请确认文件可解码；原始文件未修改")
    return result


def probe(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("找不到待分析视频")
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        metadata = next(reader)
    finally:
        reader.close()
    seconds = metadata.get("duration", 0)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("无法测得视频时长")
    # ffmpeg -i intentionally exits nonzero without an output; only inspect stream declarations.
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                            capture_output=True, timeout=20)
    stderr = result.stderr.decode("utf-8", "replace")
    return {"duration_seconds": seconds, "fps": metadata.get("fps"),
            "size": list(metadata.get("size", ())), "has_audio_stream": bool(re.search(r"Stream #.*Audio:", stderr)),
            "sha256": sha256(path)}


def windows(duration, chunk_seconds):
    result = [(round(start, 3), round(min(start + chunk_seconds, duration), 3))
              for start in range(0, math.ceil(duration), chunk_seconds) if duration - start > .001]
    if len(result) > 1 and result[-1][1] - result[-1][0] < .5:
        result[-2:] = [(result[-2][0], result[-1][1])]
    return result


def padded_bounds(start, end, duration, pad=.4):
    """Add a small context overlap while keeping the core reporting interval unchanged."""
    return max(0.0, start - pad), min(duration, end + pad)


def prepare_chunk(source, directory, start, end, has_audio):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    visual, audio = directory / "visual.mp4", directory / "audio.wav"
    length = end - start
    if not visual.is_file():
        temporary = directory / "visual.partial.mp4"
        ffmpeg(["-v", "error", "-ss", start, "-i", source, "-t", length, "-map", "0:v:0", "-an",
                "-vf", "scale=w='min(960,iw)':h='min(960,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-preset", "fast", "-crf", "25", "-pix_fmt", "yuv420p", "-y", temporary])
        temporary.replace(visual)
    if has_audio and not audio.is_file():
        temporary = directory / "audio.partial.wav"
        ffmpeg(["-v", "error", "-ss", start, "-i", source, "-t", length, "-map", "0:a:0", "-vn",
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", temporary])
        temporary.replace(audio)
    return visual, audio if has_audio else None


def render_record(path, metadata):
    path = Path(path).with_suffix(".render.json")
    if not path.is_file():
        return {"status": "missing", "note": "无生成记录，仍可分析成片；不能核对计划字幕和镜头安排"}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if abs(float(data["duration_seconds"]) - metadata["duration_seconds"]) > .5:
            raise ValueError()
        return {"status": "duration_matched_not_independently_verified", "data": data,
                "note": "同名记录时长匹配；记录不是实际视觉或听觉识别结果"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "mismatch_or_invalid", "note": "生成记录无效或时长不匹配，未用于判断"}


def snapshot(source, path, seconds, crop=None):
    filters = []
    if crop:
        # Fractions are normalized [x,y,width,height] relative to the actual video frame.
        x, y, width, height = crop
        if min(x, y) < 0 or min(width, height) <= 0 or x + width > 1 or y + height > 1:
            raise ValueError("裁剪区域超出画面")
        filters.append(f"crop=iw*{width}:ih*{height}:iw*{x}:ih*{y}")
    filters.append("scale=960:-2")
    ffmpeg(["-v", "error", "-ss", seconds, "-i", source, "-frames:v", "1", "-vf", ",".join(filters), "-y", path])
