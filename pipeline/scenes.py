"""Scene boundary detection with PySceneDetect (cached per movie)."""

import json
from pathlib import Path


def detect(movie: Path, workdir: Path, cfg: dict, log=print):
    threshold = cfg["scenes"]["threshold"]
    cache = workdir / "scenes.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        if isinstance(data, dict) and data.get("threshold") == threshold:
            scenes = [tuple(x) for x in data["scenes"]]
            log(f"{len(scenes)} scenes (cached)")
            return scenes
        log("Cached scenes used a different threshold — re-detecting.")

    from scenedetect import detect as sd_detect, ContentDetector
    scene_list = sd_detect(str(movie), ContentDetector(threshold=threshold))
    scenes = [(s[0].get_seconds(), s[1].get_seconds()) for s in scene_list]
    cache.write_text(json.dumps({"threshold": threshold, "scenes": scenes}))
    log(f"{len(scenes)} scenes detected")
    return scenes


def scene_containing(t: float, scenes):
    for start, end in scenes:
        if start <= t < end:
            return (start, end)
    return None
