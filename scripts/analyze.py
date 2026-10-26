"""Collect auditable evidence from one video URL or local file."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import subprocess
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def resolve_tool(name: str, variable: str) -> Path:
    configured = os.environ.get(variable)
    if configured:
        return Path(configured).expanduser()
    executable = name + (".exe" if os.name == "nt" else "")
    beside_python = Path(sys.executable).parent / executable
    return beside_python if beside_python.exists() else Path(shutil.which(name) or executable)


TOOLS = Path(os.environ.get("VIDEO_READER_HOME", str(Path.cwd() / ".runtime")))
YTDLP = resolve_tool("yt-dlp", "VIDEO_READER_YTDLP")
WHISPER = resolve_tool("whisper-ctranslate2", "VIDEO_READER_WHISPER")
FFMPEG_DIR = Path(os.environ.get("VIDEO_READER_FFMPEG_DIR", str(resolve_tool("ffmpeg", "VIDEO_READER_FFMPEG").parent)))
TIME_RE = re.compile(r"(\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->")
YTDLP_NETWORK = ["--ignore-config", "--socket-timeout", "30", "--retries", "3", "--extractor-retries", "3"]


def command(argv: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"命令运行超过 {timeout} 秒")


def pick_caption(info: dict, language_order: list[str]) -> tuple[str, str] | None:
    choices = []
    for source, field in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        for code, formats in (info.get(field) or {}).items():
            if not formats:
                continue
            rank = next((i for i, preferred in enumerate(language_order)
                         if code.lower().startswith(preferred.lower())), len(language_order))
            choices.append((rank, 0 if source == "manual" else 1, code, source))
    if not choices:
        return None
    _, _, code, source = min(choices)
    return code, source


def parse_caption(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    seen = ""
    current_time = ""
    current_lines = []

    def flush() -> None:
        nonlocal seen, current_lines
        clean = html.unescape(re.sub(r"<[^>]+>", "", " ".join(current_lines))).strip()
        clean = re.sub(r"\s+", " ", clean)
        if clean and clean != seen:
            cues.append({"time": current_time, "text": clean})
            seen = clean
        current_lines = []

    for line in text.splitlines() + [""]:
        line = line.strip()
        if TIME_RE.search(line):
            flush()
            current_time = line.split("-->", 1)[0].strip()
        elif not line:
            flush()
        elif current_time and line not in {"WEBVTT"} and not line.isdigit() and not line.startswith("NOTE"):
            current_lines.append(line)
    return cues


def get_info(url: str) -> tuple[dict | None, str | None]:
    if not YTDLP.exists():
        return None, f"yt-dlp 未安装：{YTDLP}"
    result = command([str(YTDLP), *YTDLP_NETWORK, "--dump-single-json", "--skip-download",
                      "--no-playlist", "--no-warnings", url], timeout=120)
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        info = None
    if not isinstance(info, dict):
        return None, result.stderr.strip() or "无法取得视频元数据"
    return info, None


def download_caption(url: str, info: dict, out: Path, languages: list[str]) -> tuple[Path | None, str | None, str | None]:
    choice = pick_caption(info, languages)
    if not choice:
        return None, None, None
    code, source = choice
    prefix = f"caption_{datetime.now():%Y%m%d_%H%M%S_%f}"
    args = [str(YTDLP), *YTDLP_NETWORK, "--skip-download", "--no-playlist", "--no-progress",
            "--sub-langs", code, "--sub-format", "vtt/srt/best",
            "-o", str(out / f"{prefix}.%(ext)s")]
    args.append("--write-subs" if source == "manual" else "--write-auto-subs")
    args.append(url)
    result = command(args, timeout=180)
    paths = sorted(out.glob(f"{prefix}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return None, source, result.stderr.strip() or "字幕列出但下载失败"
    return paths[0], source, result.stderr.strip() if result.returncode else None


def download_video(url: str, directory: Path) -> tuple[Path | None, str | None]:
    args = [str(YTDLP), *YTDLP_NETWORK, "--no-playlist", "--no-progress", "--max-filesize", "400M",
            "--ffmpeg-location", str(FFMPEG_DIR),
            "-f", "bv*[height<=720]+ba/b[height<=720]/best",
            "-o", str(directory / "media.%(ext)s"), url]
    result = command(args, timeout=900)
    if result.returncode:
        return None, result.stderr.strip() or "视频下载未完成"
    files = [p for p in directory.glob("media.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        return None, result.stderr.strip() or "视频下载失败"
    return max(files, key=lambda p: p.stat().st_size), None


def sample_frames(video: Path, out: Path, chapters: list[dict], maximum: int, duration_hint: float | None,
                  probe_limit: int = 180, fast: bool = False) -> tuple[list[dict], str | None, float | None]:
    try:
        import cv2
        from rapidocr import RapidOCR
    except ImportError as exc:
        return [], f"画面处理组件不可用：{exc}", None

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return [], "视频解码失败", None
    fps = cap.get(cv2.CAP_PROP_FPS)
    count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration = count / fps if fps and count else duration_hint
    if not duration or duration <= 0:
        cap.release()
        return [], "无法确定时长，未抽样画面", None

    def frame_at(sec: float):
        cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)
        ok, frame = cap.read()
        return frame if ok else None

    if fast:
        chapter_times = [float(c["start_time"]) for c in chapters
                         if isinstance(c.get("start_time"), (int, float)) and 0 <= c["start_time"] < duration]
        if chapter_times:
            selected = [0.0, max(0.0, duration - 0.1)]
            for sec in chapter_times:
                if all(abs(sec - x) >= 3 for x in selected):
                    selected.append(sec)
            selected = sorted(selected[:maximum])
            while len(selected) < maximum:
                gaps = [(b - a, a, b) for a, b in zip(selected, selected[1:])]
                if not gaps:
                    break
                width, a, b = max(gaps)
                if width < 0.5:
                    break
                selected.append((a + b) / 2)
                selected.sort()
        else:
            selected = [i * max(0.0, duration - 0.1) / max(1, maximum - 1)
                        for i in range(maximum)]
    else:
        probe_count = min(probe_limit, max(2, math.ceil(duration / 2)))
        probes = []
        previous = None
        for i in range(probe_count):
            sec = max(0.0, min(duration - 0.05, i * duration / probe_count))
            frame = frame_at(sec)
            if frame is None:
                continue
            thumb = cv2.resize(frame, (96, 54))
            change = 0.0 if previous is None else float(cv2.absdiff(thumb, previous).mean())
            probes.append((sec, change))
            previous = thumb

        candidates = [(0.0, 1000.0), (max(0.0, duration - 0.1), 900.0)]
        for chapter in chapters:
            start = chapter.get("start_time")
            if isinstance(start, (int, float)) and 0 <= start < duration:
                candidates.append((float(start), 800.0))
        for sec, change in sorted(probes, key=lambda x: x[1], reverse=True)[:maximum]:
            candidates.append((sec, 100.0 + change))
        for i in range(maximum):
            candidates.append((min(duration - 0.05, (i + 0.5) * duration / maximum), 10.0))

        selected = []
        separation = min(3.0, max(0.5, duration / maximum / 4))
        for sec, score in sorted(candidates, key=lambda x: x[1], reverse=True):
            if all(abs(sec - old) >= separation for old in selected):
                selected.append(sec)
            if len(selected) >= maximum:
                break
        selected.sort()
    out.mkdir(parents=True, exist_ok=True)
    try:
        ocr = RapidOCR()
        ocr_init_error = None
    except Exception as exc:
        ocr = None
        ocr_init_error = str(exc)
    records = []
    for i, sec in enumerate(selected, 1):
        frame = frame_at(sec)
        if frame is None:
            continue
        path = out / f"frame_{i:03d}_{int(sec):06d}s.jpg"
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            continue
        encoded.tofile(str(path))
        if ocr is None:
            lines, error = [], ocr_init_error
        else:
            try:
                result = ocr(str(path))
                lines = [{"text": text, "score": round(float(score), 4)}
                         for text, score in zip(result.txts or (), result.scores or ())]
                error = None
            except Exception as exc:
                lines, error = [], str(exc)
        records.append({"seconds": round(sec, 2), "path": str(path), "ocr": lines, "ocr_error": error})
    cap.release()
    return records, None, duration


def transcribe(video: Path, out: Path, model: str, language: str | None) -> tuple[list[dict], str | None]:
    if not WHISPER.exists():
        return [], f"语音转写工具不可用：{WHISPER}"
    out.mkdir(parents=True, exist_ok=True)
    args = [str(WHISPER), str(video), "--model", model, "--device", "cpu",
            "--compute_type", "int8", "--output_dir", str(out),
            "--output_format", "json", "--verbose", "False"]
    if language:
        args.extend(["--language", language])
    result = command(args, timeout=1800)
    if result.returncode:
        return [], result.stderr.strip() or "转写未完成"
    path = out / f"{video.stem}.json"
    if not path.exists():
        return [], result.stderr.strip() or "转写未生成结果"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        segments = [{"start": round(float(s["start"]), 2),
                     "end": round(float(s["end"]), 2), "text": s["text"].strip()}
                    for s in data.get("segments", []) if s.get("text", "").strip()]
        return segments, None
    except (ValueError, KeyError) as exc:
        return [], f"转写结果无法解析：{exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="提取单个视频的元数据、章节、字幕、画面 OCR 和可选语音转写")
    parser.add_argument("source", help="单个 http(s) 视频链接或本地视频文件")
    parser.add_argument("--out", type=Path, help="证据输出目录")
    parser.add_argument("--visual", action="store_true", help="有字幕时也抽样检查画面")
    parser.add_argument("--asr", action="store_true", help="需要口述内容时转写整段音频")
    parser.add_argument("--asr-if-no-caption", action="store_true", help="仅在无法取得字幕时转写音频")
    parser.add_argument("--model", default="base", help="Whisper 模型，默认 base")
    parser.add_argument("--asr-language", help="语音语言代码，例如 zh 或 en")
    parser.add_argument("--caption-languages", default="zh,en", help="优先字幕语言，逗号分隔")
    parser.add_argument("--max-frames", type=int, help="最多抽样画面数；默认 24，快速模式默认 8")
    parser.add_argument("--fast", action="store_true", help="快速检查：默认抽样 8 帧、减少场景探测，并与转写并行")
    parser.add_argument("--audio-source", type=Path, help="独立音轨文件；避免先与视频合并")
    args = parser.parse_args()
    if args.max_frames is None:
        args.max_frames = 8 if args.fast else 24
    if args.audio_source and not args.audio_source.is_file():
        parser.error("--audio-source 必须是现有音频文件")
    source = args.source
    local = Path(source).is_file()
    if not local and urlparse(source).scheme not in {"http", "https"}:
        parser.error("source 必须是本地文件或 http(s) 链接")
    if not 1 <= args.max_frames <= 120:
        parser.error("--max-frames 应在 1 到 120 之间")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    out = args.out or TOOLS / "results" / f"{digest}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    report = {"source": source, "created_utc": datetime.now(timezone.utc).isoformat(),
              "metadata": None, "chapters": [], "caption": None, "caption_segments": [],
              "asr_segments": [], "frames": [], "acquisition": {}, "warnings": [],
              "coverage": {"browser_checked": False, "motion_checked": False}}

    info = None
    if local:
        report["acquisition"]["local_video"] = str(Path(source).resolve())
    else:
        info, error = get_info(source)
        if error:
            report["warnings"].append(error)
        if info:
            report["metadata"] = {k: info.get(k) for k in
                                  ("id", "title", "uploader", "channel", "creator", "duration",
                                   "upload_date", "description", "webpage_url", "extractor")}
            report["chapters"] = info.get("chapters") or []
            report["acquisition"]["metadata"] = "yt-dlp"
            report["acquisition"]["available_subtitle_languages"] = list((info.get("subtitles") or {}).keys())
            report["acquisition"]["available_auto_caption_languages"] = list((info.get("automatic_captions") or {}).keys())
            caption, kind, error = download_caption(source, info, out, args.caption_languages.split(","))
            if error:
                report["warnings"].append(error)
            if caption:
                report["caption"] = {"kind": kind, "path": str(caption)}
                report["caption_segments"] = parse_caption(caption)
                report["acquisition"]["caption"] = kind

    asr_requested = args.asr or (args.asr_if_no_caption and not report["caption"])
    need_video = local or args.visual or asr_requested or not report["caption"]
    if need_video:
        with tempfile.TemporaryDirectory(prefix="video-link-reader-") as scratch:
            if local:
                video, error = Path(source).resolve(), None
            elif info:
                video, error = download_video(source, Path(scratch))
                if video:
                    report["acquisition"]["temporary_video_bytes"] = video.stat().st_size
            else:
                video, error = None, "元数据失败，未尝试视频下载"
            if error:
                report["warnings"].append(error)
            if video:
                audio = args.audio_source.resolve() if args.audio_source else video
                if args.audio_source:
                    report["acquisition"]["local_audio"] = str(audio)
                if args.fast and asr_requested:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        frame_job = pool.submit(sample_frames, video, out / "frames", report["chapters"],
                                                args.max_frames, (info or {}).get("duration"), 24, True)
                        asr_job = pool.submit(transcribe, audio, out / "asr", args.model, args.asr_language)
                        frames, error, duration = frame_job.result()
                        segments, asr_error = asr_job.result()
                else:
                    frames, error, duration = sample_frames(video, out / "frames", report["chapters"],
                                                            args.max_frames, (info or {}).get("duration"),
                                                            24 if args.fast else 180, args.fast)
                report["frames"] = frames
                report["coverage"]["duration_seconds"] = duration
                report["coverage"]["sampled_frame_count"] = len(frames)
                report["coverage"]["visual_method"] = (
                    "章节锚点或时间均匀抽样；逐帧 RapidOCR" if args.fast
                    else "时间均匀抽样 + 缩略图变化筛选；逐帧 RapidOCR"
                )
                if error:
                    report["warnings"].append(error)
                if asr_requested:
                    if not args.fast:
                        segments, asr_error = transcribe(audio, out / "asr", args.model, args.asr_language)
                    report["asr_segments"] = segments
                    report["acquisition"]["asr"] = f"whisper-ctranslate2/{args.model}"
                    if asr_error:
                        report["warnings"].append(asr_error)
                if args.fast:
                    report["coverage"]["fast_mode"] = True

    transcript = report["caption_segments"] or report["asr_segments"]
    if transcript:
        (out / "transcript.txt").write_text(
            "\n".join(f"[{s.get('time', s.get('start', ''))}] {s['text']}" for s in transcript),
            encoding="utf-8",
        )
    report["coverage"]["note"] = (
        "字幕或转写按取得的完整文件处理；画面只检查列出的抽样帧，未逐帧观看，也未核对连续动效。"
        if report["frames"] else "未检查视频画面或连续动效。"
    )
    path = out / "evidence.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(path))
    return 0 if report["metadata"] or local else 1


if __name__ == "__main__":
    sys.exit(main())
