"""Sexual content pass: nominate frames with two models, confirm with a
third -> SKIP ranges.

Every sampled frame is scored by NudeNet (body-part detector) and a ViT NSFW
classifier. Either one can nominate a frame; CLIP then decides whether the
frame actually shows a naked body rather than a clothed or shirtless one
(see verify.py for why one model alone isn't enough). Only confirmed frames
become cuts.

Enabled categories come from the Settings GUI
(cfg["nsfw"]["labels"][LABEL] = {enabled, threshold}). With verification on,
a category's threshold only controls how readily NudeNet nominates frames,
capped at VERIFY_DEFAULTS["nudenet_nominate"] so a high threshold can't hide
nudity that CLIP would confirm.

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

from . import extract, verify
from .scenes import scene_containing

# Calibrated on hand-labelled frames from real titles. Override any of them
# under nsfw.verify in config.yaml; nsfw.verify.enabled: false restores the
# old NudeNet-only pass.
VERIFY_DEFAULTS = {
    "enabled": True,
    "vit_nominate": 0.30,      # classifier score that nominates a frame
    "nudenet_nominate": 0.35,  # NudeNet nominates at min(category threshold, this)
    "clip_confirm": 0.86,      # CLIP nudity score required to cut
}

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

    vcfg = {**VERIFY_DEFAULTS, **(ncfg.get("verify") or {})}

    detector = _get_detector(log)
    verifier = verify.load(log) if vcfg["enabled"] else None
    if vcfg["enabled"] and verifier is None:
        log("Verification models unavailable — falling back to NudeNet "
            "alone for this run (expect more false positives).")
    nominate_at = ({k: min(v, vcfg["nudenet_nominate"]) for k, v in triggers.items()}
                   if verifier else triggers)

    fps = assets["frame_fps"]
    frames = sorted(assets["frames_dir"].glob("f_*.jpg"))
    log(f"Classifying {len(frames)} frames "
        f"({len(triggers)} categories active)...")

    candidates, unreadable = [], 0
    batch = 32
    for i in range(0, len(frames), batch):
        chunk = []
        for path in frames[i:i + batch]:
            r = verify.read_frame(path)
            if r is None:
                unreadable += 1
            else:
                chunk.append((path, *r))
        if chunk:
            dets = _detect(detector, [data for _, data, _ in chunk])
            vits = (verifier.vit_scores([img for _, _, img in chunk])
                    if verifier else [0.0] * len(chunk))
            for (path, _, _), ds, v in zip(chunk, dets, vits):
                hit = _best_hit(ds, nominate_at)
                if hit or (verifier and v >= vcfg["vit_nominate"]):
                    candidates.append({
                        "t": extract.frame_time(path, fps), "frame": str(path),
                        "label": hit[0] if hit else None,
                        "nudenet": round(hit[1], 3) if hit else 0.0,
                        "vit": round(float(v), 3)})
        if progress:
            progress(min(1.0, (i + batch) / len(frames)))
    if unreadable:
        log(f"Skipped {unreadable} unreadable frame(s).")

    if verifier:
        log(f"{len(candidates)} frames nominated — confirming each with CLIP...")
        for c in candidates:
            r = verify.read_frame(Path(c["frame"]))
            c["clip"] = round(verifier.clip_nudity(r[1]), 3) if r else 0.0
            c["confirmed"] = c["clip"] >= vcfg["clip_confirm"]
        flagged = [(c["t"], c["label"] or "NUDITY", c["clip"], c["frame"])
                   for c in candidates if c["confirmed"]]
        log(f"{len(flagged)} confirmed, {len(candidates) - len(flagged)} "
            "rejected as false alarms")
    else:
        flagged = [(c["t"], c["label"], c["nudenet"], c["frame"]) for c in candidates]
        log(f"{len(flagged)} frames flagged")

    ranges = _frames_to_ranges(flagged, scene_list, ncfg, fps)
    _log_json(assets["workdir"], flagged, ranges, candidates)
    return ranges


def _detect(detector, items):
    """NudeNet over a chunk; if one item makes the batch fail, retry item by
    item so a single bad frame only loses itself."""
    try:
        return detector.detect_batch(items)
    except Exception:
        out = []
        for item in items:
            try:
                out.append(detector.detect(item))
            except Exception:
                out.append([])
        return out


def _best_hit(dets, thresholds):
    """(label, score) of the strongest detection at or above its category's
    threshold, or None."""
    hits = [(d["class"], d["score"]) for d in dets
            if d["class"] in thresholds and d["score"] >= thresholds[d["class"]]]
    return max(hits, key=lambda h: h[1]) if hits else None


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


def _log_json(workdir: Path, flagged, ranges, candidates=()):
    log = workdir / "analysis.json"
    data = json.loads(log.read_text()) if log.exists() else {}
    data["nsfw"] = {
        "flagged_frames": [{"t": t, "label": l, "score": round(s, 3), "frame": f}
                           for t, l, s, f in flagged],
        "ranges": ranges,
        # every nominated frame with all three scores, confirmed or not
        "candidates": list(candidates),
    }
    log.write_text(json.dumps(data, indent=1))
