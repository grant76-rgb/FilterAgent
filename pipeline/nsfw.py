"""Sexual content pass: NudeNet frame classification -> SKIP ranges.

Thresholds and enabled categories come from the Settings GUI
(cfg["nsfw"]["labels"][LABEL] = {enabled, threshold}).

Uses NudeNet's larger "640m" model (YOLOv8-medium @ 640x640) instead of the
"320n" nano model bundled with the package -- meaningfully more accurate at
the cost of slower per-frame inference. Not bundled with the pip package, so
it's downloaded once to %APPDATA%\\FilterAgent\\models\\ on first use and
cached there; falls back to the bundled 320n model if the download fails.
"""

import json
import os
import shutil
import urllib.request
from collections import defaultdict
from pathlib import Path

from . import extract
from .scenes import scene_containing

MODEL_URL = "https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx"
MIN_MODEL_BYTES = 50_000_000  # real file is ~100 MB; guards against an HTML/error response


def _model_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    d = base / "FilterAgent" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d / "640m.onnx"


def _download_model(dest: Path):
    """Downloads to a .part file and only renames it into place once its size
    looks like a real model file — some hosts reject/redirect bare Python
    user agents, and a silently-cached HTML error page would break every
    run after it."""
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "Mozilla/5.0"})
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    size = tmp.stat().st_size
    if size < MIN_MODEL_BYTES:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded file too small ({size} bytes) — "
                           "likely blocked or redirected")
    tmp.rename(dest)


def _get_detector(log=print):
    from nudenet import NudeDetector

    path = _model_path()
    if not path.exists():
        log("Downloading higher-accuracy detection model (640m, ~100 MB, "
            "one-time)...")
        try:
            _download_model(path)
        except Exception as e:
            log(f"Model download failed ({e}) — using the smaller built-in "
                "model for this run.")
            return NudeDetector()

    try:
        return NudeDetector(model_path=str(path), inference_resolution=640)
    except Exception as e:
        log(f"Failed to load the 640m model ({e}) — discarding it and using "
            "the smaller built-in model for this run.")
        path.unlink(missing_ok=True)
        return NudeDetector()


def scan(assets: dict, scene_list, cfg: dict, log=print, progress=None) -> list:
    ncfg = cfg["nsfw"]
    triggers = {label: spec["threshold"]
                for label, spec in ncfg["labels"].items() if spec["enabled"]}
    if not triggers:
        log("No sexual-content categories enabled — skipping.")
        return []

    detector = _get_detector(log)

    fps = assets["frame_fps"]
    frames = sorted(assets["frames_dir"].glob("f_*.jpg"))
    log(f"Classifying {len(frames)} frames "
        f"({len(triggers)} categories active)...")

    flagged = []
    batch = 32
    for i in range(0, len(frames), batch):
        chunk = frames[i:i + batch]
        results = detector.detect_batch([str(p) for p in chunk])
        for path, dets in zip(chunk, results):
            t = extract.frame_time(path, fps)
            for d in dets:
                thr = triggers.get(d["class"])
                if thr is not None and d["score"] >= thr:
                    flagged.append((t, d["class"], d["score"], str(path)))
                    break
        if progress:
            progress(min(1.0, (i + batch) / len(frames)))

    log(f"{len(flagged)} frames flagged")
    ranges = _frames_to_ranges(flagged, scene_list, ncfg, fps)
    _log_json(assets["workdir"], flagged, ranges)
    return ranges


def _frames_to_ranges(flagged, scene_list, ncfg, fps) -> list:
    if not flagged:
        return []
    ratio = ncfg.get("scene_skip_ratio", 0.3)
    pad = ncfg.get("span_pad", 2.0)

    by_scene = defaultdict(list)
    orphans = []
    for t, label, score, path in flagged:
        sc = scene_containing(t, scene_list)
        (by_scene[sc] if sc else orphans).append((t, label, score))

    ranges = []
    for key, hits in by_scene.items():
        s, e = key
        scene_frames = max(1, int((e - s) * fps))
        if len(hits) / scene_frames >= ratio:
            start, end = max(0, s - pad), e
        else:
            ts = [h[0] for h in hits]
            start, end = min(ts) - pad, max(ts) + pad
        top = max(hits, key=lambda h: h[2])
        ranges.append({
            "start": max(0, start), "end": end, "action": 0,
            "reason": f"nsfw: {top[1]} ({len(hits)} frames)",
            "confidence": top[2],
        })
    for t, label, score in orphans:
        ranges.append({
            "start": max(0, t - pad), "end": t + pad, "action": 0,
            "reason": f"nsfw: {label} (isolated)", "confidence": score,
        })
    return ranges


def _log_json(workdir: Path, flagged, ranges):
    log = workdir / "analysis.json"
    data = json.loads(log.read_text()) if log.exists() else {}
    data["nsfw"] = {
        "flagged_frames": [{"t": t, "label": l, "score": round(s, 3), "frame": f}
                           for t, l, s, f in flagged],
        "ranges": ranges,
    }
    log.write_text(json.dumps(data, indent=1))
