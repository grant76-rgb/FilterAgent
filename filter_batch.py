#!/usr/bin/env python3
"""
filter_batch.py -- filter a whole season (or any set of files) unattended:
the right-click "Create filter EDL", with your saved settings, run on one
episode after another.

USAGE
    python filter_batch.py "C:\\TV\\Marvel's Daredevil\\Season 2"
      Lists what it's going to do, then carries on in the background, so
      the window can be closed. Episodes run one at a time at below-normal
      priority; the PC is kept from going to sleep until the last one is
      done, and a Windows notification says when that is. Progress goes to
      %APPDATA%\\FilterAgent\\batch.log.

  Several files/folders can be given at once; folders are searched
  recursively. Episodes that already have an .edl are skipped, so running
  the same command again after an interruption picks up where it stopped.
  So are files apply_edl.py has already filtered, and an episode another
  FilterAgent run is still working on.

  Options: --profanity-only  --dry-run  --wait  --log FILE
"""

import argparse
import collections
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import app
from apply_edl import MARK, VIDEO_EXTS, busy_movies, probe, tag

TOOL_DIR = Path(__file__).resolve().parent
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000   # outlive a terminal that kills what it started
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001          # no idle sleep; the screen may still turn off


def say(msg=""):
    print(msg, flush=True)


def python_exe(name: str) -> str:
    """python.exe / pythonw.exe of the interpreter running this script."""
    exe = Path(sys.executable).with_name(name)
    return str(exe if exe.exists() else sys.executable)


def open_log(path: Path, echo: bool):
    out = open(path, "a", encoding="utf-8", buffering=1)

    def log(msg):
        line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
        out.write(line + "\n")
        if echo:
            say(line)
    return log, out


def find_movies(targets) -> list:
    found = []
    for t in targets:
        if t.is_dir():
            found += sorted(p for p in t.rglob("*") if p.suffix.lower() in VIDEO_EXTS
                            and ".filteragent" not in p.parts)
        else:
            found.append(t)
    return list(dict.fromkeys(p.resolve() for p in found))


def skip_reason(movie: Path):
    if movie.with_suffix(".edl").exists():
        return "already has an EDL"
    try:
        info = probe(movie)
    except (RuntimeError, ValueError):
        return "ffprobe can't read it"
    if tag(info["format"], MARK):
        return "already filtered by apply_edl.py"
    return None


def run_episode(movie: Path, cfg: dict, log) -> Path:
    """Runs one episode in the same worker process the progress window uses,
    so apply_edl.py sees it as busy and a crash can't take the batch down.
    Returns the .edl path, or raises with the reason it failed."""
    from gui import proctree
    fd, cfg_path = tempfile.mkstemp(prefix="filteragent_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    proc = subprocess.Popen(
        [python_exe("python.exe"), "-m", "gui.worker", str(movie), cfg_path],
        cwd=TOOL_DIR, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
    proctree.bind_to_this_process(int(proc._handle))   # it and its ffmpeg die with the batch

    # keep draining stderr, or a chatty library fills the pipe and the worker blocks
    tail = collections.deque(maxlen=30)
    reader = threading.Thread(target=lambda: tail.extend(
        line.decode("utf-8", "replace") for line in proc.stderr), daemon=True)
    reader.start()

    tenth, result = -1, None
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind, payload = ev["kind"], ev["payload"]
        if kind == "log":
            log(f"    {payload}")
        elif kind == "frameprog" and int(payload * 10) > tenth:
            tenth = int(payload * 10)
            log(f"    frames {payload:.0%}")
        elif kind in ("done", "error"):
            result = (kind, payload)
    code = proc.wait()
    reader.join(5)
    if result and result[0] == "done":
        return Path(result[1][0])
    if result:
        raise RuntimeError(result[1].rstrip())
    raise RuntimeError(f"the worker stopped unexpectedly (exit code {code})\n"
                       + "".join(tail).rstrip())


def run_batch(targets, args, log) -> dict:
    from gui.notify import notify
    k32 = ctypes.windll.kernel32
    awake = k32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    movies = find_movies(targets)
    log(f"Batch of {len(movies)} file(s) starting (pid {os.getpid()})"
        + ("" if awake else " -- Windows refused to keep the PC awake"))
    counts = {"filtered": 0, "skipped": 0, "failed": 0}
    try:
        for i, movie in enumerate(movies, 1):
            label = f"[{i}/{len(movies)}] {movie.name}"
            try:
                # checked again here: a right-click run may have finished it meanwhile
                why = skip_reason(movie)
                if not why and os.path.normcase(str(movie)) in busy_movies():
                    why = "another FilterAgent run is working on it"
                if why:
                    log(f"{label}: skipped, {why}")
                    counts["skipped"] += 1
                    continue
                cfg = app.load_config()    # re-read, so Settings changes apply from the next episode
                if args.profanity_only:
                    cfg["nsfw"]["enabled"] = False
                log(f"{label}: starting")
                t0 = time.time()
                edl = run_episode(movie, cfg, log)
                counts["filtered"] += 1
                log(f"{label}: DONE in {(time.time() - t0) / 60:.0f} min -> {edl.name}")
            except Exception as e:
                counts["failed"] += 1
                log(f"{label}: FAILED\n{e}")
    finally:
        k32.SetThreadExecutionState(ES_CONTINUOUS)
        summary = (f"{counts['filtered']} filtered, {counts['skipped']} skipped, "
                   f"{counts['failed']} failed")
        log(f"Batch finished: {summary}. The PC can sleep again.")
        if counts["filtered"] or counts["failed"]:
            notify("FilterAgent", f"Batch finished: {summary}.")
    return counts


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Filter every episode in a folder, unattended.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("USAGE", 1)[1])
    ap.add_argument("paths", nargs="+", metavar="path",
                    help="episode file(s) and/or folder(s); folders are searched recursively")
    ap.add_argument("--profanity-only", action="store_true",
                    help="skip scene detection and the sexual-content filter, like the "
                         "right-click entry of that name (saved settings are untouched)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be filtered, then stop")
    ap.add_argument("--wait", action="store_true",
                    help="run in this window, showing progress, instead of in the background "
                         "(closing the window then stops it)")
    ap.add_argument("--log", type=Path, default=app.config_dir() / "batch.log",
                    help="progress log, appended to (default: %(default)s)")
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    targets = [Path(p).resolve() for p in args.paths]
    missing = [str(t) for t in targets if not t.exists()]
    if missing:
        ap.error("not found: " + ", ".join(missing))
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        ap.error("ffmpeg/ffprobe not found on PATH")

    if args.background:
        log, out = open_log(args.log, echo=False)
        sys.stdout = sys.stderr = out      # no console under pythonw; tracebacks land in the log
        run_batch(targets, args, log)
        return

    movies = find_movies(targets)
    todo = 0
    for m in movies:
        why = skip_reason(m)
        say(f"  skip    {m.name} ({why})" if why else f"  filter  {m.name}")
        todo += not why
    say(f"\n{todo} to filter, {len(movies) - todo} skipped.")
    if args.dry_run or not todo:
        return

    if args.wait:
        log, _ = open_log(args.log, echo=True)
        sys.exit(1 if run_batch(targets, args, log)["failed"] else 0)

    # hand over to a windowless copy of this script that outlives this window
    cmd = [python_exe("pythonw.exe"), str(Path(__file__).resolve()), "--background",
           "--log", str(args.log.resolve())]
    if args.profanity_only:
        cmd.append("--profanity-only")
    cmd += [str(t) for t in targets]
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    popen = dict(cwd=TOOL_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL)
    try:
        proc = subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **popen)
    except OSError:                        # this window's job doesn't allow breaking away
        proc = subprocess.Popen(cmd, creationflags=flags, **popen)
    say("Filtering in the background -- you can close this window.")
    say(f"  progress:  {args.log.resolve()}")
    say(f"  to stop:   taskkill /F /PID {proc.pid}")
    say("A notification pops up when it's done.")


if __name__ == "__main__":
    main()
