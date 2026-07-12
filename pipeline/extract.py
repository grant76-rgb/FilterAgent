"""Extract subtitles, audio, and sampled frames using ffmpeg (must be on PATH)."""

import json
import shutil
import subprocess
from pathlib import Path

# Prevent console windows popping up when launched from pythonw.exe
CREATE_NO_WINDOW = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False,
                          creationflags=CREATE_NO_WINDOW)


def _download_subtitle(movie: Path, srt_path: Path, cfg: dict, log) -> bool:
    """Try to fetch a subtitle online (via subliminal) and save it to
    srt_path. Returns True on success."""
    scfg = cfg.get("subtitles", {})
    if not scfg.get("auto_download", True):
        return False

    try:
        import subliminal
        from babelfish import Language
    except ImportError:
        log("subliminal not installed — skipping subtitle download "
            "(pip install subliminal)")
        return False

    log("No local subtitles found — searching online for subtitles...")
    try:
        langs = {Language(code) for code in scfg.get("languages", ["eng"])}
        video = subliminal.scan_video(str(movie))
        found = subliminal.download_best_subtitles({video}, langs)
        subs = found.get(video)
        if not subs:
            log("No subtitles found online.")
            return False
        saved = subliminal.save_subtitles(video, subs, single=True,
                                          directory=str(srt_path.parent))
        if not saved:
            return False
        saved_name = Path(saved[0].get_path(video, single=True)).name
        (srt_path.parent / saved_name).replace(srt_path)
        log(f"Downloaded subtitle from {subs[0].provider_name}")
        return True
    except Exception as e:
        log(f"Subtitle download failed: {e}")
        return False


def probe(movie: Path) -> dict:
    r = _run(["ffprobe", "-v", "quiet", "-print_format", "json",
              "-show_streams", "-show_format", str(movie)])
    return json.loads(r.stdout)


def run(movie: Path, workdir: Path, cfg: dict, log=print) -> dict:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install it (e.g. 'winget install "
            "ffmpeg') and restart.")

    info = probe(movie)
    duration = float(info["format"]["duration"])
    log(f"Duration: {duration/60:.1f} min")

    # --- Subtitles -----------------------------------------------------------
    srt_path = workdir / "subs.srt"
    if not srt_path.exists():
        sub_streams = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
        text_subs = [s for s in sub_streams
                     if s.get("codec_name") in ("subrip", "ass", "ssa", "mov_text")]

        def score(s):
            tags = s.get("tags", {})
            forced = s.get("disposition", {}).get("forced", 0)
            return (tags.get("language", "").startswith("en"), not forced)
        text_subs.sort(key=score, reverse=True)

        if text_subs:
            idx = text_subs[0]["index"]
            _run(["ffmpeg", "-y", "-i", str(movie), "-map", f"0:{idx}",
                  str(srt_path)])
            log(f"Extracted subtitle track #{idx}")
        else:
            external = movie.with_suffix(".srt")
            if external.exists():
                srt_path.write_text(
                    external.read_text(errors="replace"), encoding="utf-8")
                log("Using external .srt next to the movie")
            elif not _download_subtitle(movie, srt_path, cfg, log):
                log("WARNING: no text subtitles found — profanity pass will "
                    "be skipped (or enable Whisper alignment in Settings).")
                srt_path = None

    # --- Audio ---------------------------------------------------------------
    wav_path = workdir / "audio.wav"
    pcfg = cfg["profanity"]
    need_whisper = (pcfg.get("use_whisper_alignment")
                     or (srt_path is None and pcfg.get("whisper_fallback", True)))
    if need_whisper and not wav_path.exists():
        log("Extracting audio for Whisper...")
        _run(["ffmpeg", "-y", "-i", str(movie), "-vn",
              "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)])

    # --- Frames ----------------------------------------------------------------
    fps = cfg["sampling"]["fps"]
    frames_dir = workdir / "frames"
    fps_marker = workdir / "frames.fps"
    cached_fps = float(fps_marker.read_text()) if fps_marker.exists() else None
    if frames_dir.exists() and cached_fps != fps:
        log(f"Cached frames were sampled at {cached_fps} fps, current setting "
            f"is {fps} fps — discarding stale cache and re-sampling.")
        shutil.rmtree(frames_dir, ignore_errors=True)

    if not cfg["nsfw"]["enabled"]:
        log("Sexual content filter disabled — skipping frame sampling.")
    elif not frames_dir.exists() or not any(frames_dir.iterdir()):
        frames_dir.mkdir(exist_ok=True)
        log(f"Sampling frames at {fps} fps (this is the slow part)...")
        _run(["ffmpeg", "-y", "-i", str(movie),
              "-vf", f"fps={fps},scale=640:-2",
              "-fps_mode", "vfr",
              str(frames_dir / "f_%08d.jpg")])
        fps_marker.write_text(str(fps))
        n = len(list(frames_dir.glob("*.jpg")))
        log(f"{n} frames extracted")

    return {
        "movie": movie, "srt": srt_path, "wav": wav_path,
        "frames_dir": frames_dir, "frame_fps": fps,
        "duration": duration, "workdir": workdir,
    }


def frame_time(frame_path: Path, fps: float) -> float:
    n = int(frame_path.stem.split("_")[1])
    return (n - 1) / fps  # ffmpeg numbering starts at 1
