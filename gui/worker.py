"""Pipeline worker, run as a child process of the progress window so the
whole run — including the ffmpeg processes it starts — can be paused,
resumed and cancelled at any moment. (A thread inside the window's own
process can't be frozen safely: it may be holding the interpreter lock.)

usage (by gui/progress.py):  python -m gui.worker <movie> <config.json>

Events go to the window as one JSON object per line on stdout:
    {"kind": "log" | "stage" | "frameprog" | "done" | "error", "payload": ...}
The config file is a JSON dump of the window's (already-overridden) config;
it is deleted once read.
"""

import json
import os
import sys
import traceback
from pathlib import Path


def run(movie: Path, cfg: dict, emit):
    from pipeline import extract, profanity, scenes, nsfw, edl, subtitles, review, cleanup

    def log(m):
        emit("log", str(m))

    workdir = movie.parent / ".filteragent" / movie.stem
    workdir.mkdir(parents=True, exist_ok=True)

    emit("stage", 0)
    assets = extract.run(movie, workdir, cfg, log=log)

    ranges = []
    emit("stage", 1)
    if cfg["profanity"]["enabled"]:
        ranges += profanity.scan(assets, cfg, log=log)
    else:
        log("Profanity muting disabled in settings.")

    emit("stage", 2)
    if cfg["nsfw"]["enabled"]:
        scene_list = scenes.detect(movie, workdir, cfg, log=log)
    else:
        scene_list = []
        log("Sexual content filter disabled — skipping scene detection.")

    emit("stage", 3)
    if cfg["nsfw"]["enabled"]:
        ranges += nsfw.scan(assets, scene_list, cfg, log=log,
                            progress=lambda p: emit("frameprog", p))
    else:
        log("Sexual content filter disabled in settings.")

    emit("stage", 4)
    edl_path = edl.write(movie, ranges, workdir, cfg, log=log)

    emit("stage", 5)
    srt_path = subtitles.censor(assets, movie, cfg, log=log)

    sheet = review.build_contact_sheet(workdir, movie, cfg)

    emit("stage", 6)
    sheet = cleanup.finalize(movie, workdir, sheet,
                             delete_workdir=cfg["cleanup"].get("delete_workdir", True),
                             log=log)
    return edl_path, sheet, srt_path


def main():
    # Keep the event channel to ourselves: anything else that writes to
    # stdout (including native libraries) is pointed at stderr instead.
    channel = os.fdopen(os.dup(1), "w", encoding="ascii", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def emit(kind, payload=None):
        channel.write(json.dumps({"kind": kind, "payload": payload}) + "\n")
        channel.flush()

    movie = Path(sys.argv[1])
    cfg_file = Path(sys.argv[2])
    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    cfg_file.unlink(missing_ok=True)
    try:
        edl_path, sheet, srt_path = run(movie, cfg, emit)
        emit("done", [str(edl_path),
                      str(sheet) if sheet else None,
                      str(srt_path) if srt_path else None])
    except Exception:
        emit("error", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
