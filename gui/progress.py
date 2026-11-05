"""Progress window: runs the pipeline in a separate worker process
(gui/worker.py), streams its log, writes the EDL next to the movie,
optionally opens the review sheet.

Pause freezes the worker and every ffmpeg it started (no CPU used, memory
kept) and Resume picks up exactly where it stopped; Cancel or closing the
window ends them all. The worker runs at below-normal priority, so even
unpaused it yields the CPU to whatever else you're doing.
"""

import collections
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from gui import proctree
from gui.notify import notify

STAGES = ["Extracting", "Profanity", "Scenes", "Sexual content", "Writing EDL",
          "Censoring subtitles", "Cleaning up"]
TOOL_DIR = Path(__file__).resolve().parent.parent
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def _python() -> str:
    """The worker needs real stdout/stderr handles, so use python.exe even
    when this window was started with pythonw.exe (CREATE_NO_WINDOW keeps
    it invisible either way)."""
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    return str(console if exe.name.lower() == "pythonw.exe" and console.exists() else exe)


class ProgressWindow:
    def __init__(self, movie: Path, cfg: dict):
        self.movie = movie
        self.cfg = cfg
        self.q = queue.Queue()
        self.proc = None
        self.job = None
        self.suspended = None      # PIDs frozen by Pause, or None while running
        self.finished = False
        self.cancelled = False
        self.stage_text = "Starting..."
        self.stderr_tail = collections.deque(maxlen=30)

        self.root = tk.Tk()
        self.root.title(f"FilterAgent — {movie.name}")
        self.root.geometry("560x380")
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.stage_lbl = ttk.Label(self.root, text=self.stage_text,
                                   font=("Segoe UI", 11, "bold"))
        self.stage_lbl.pack(anchor="w", padx=10, pady=(10, 2))

        self.bar = ttk.Progressbar(self.root, maximum=len(STAGES))
        self.bar.pack(fill="x", padx=10, pady=4)

        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log = tk.Text(frame, state="disabled", font=("Consolas", 9),
                           bg="#111", fg="#ccc")
        ys = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

        buttons = ttk.Frame(self.root)
        buttons.pack(pady=(0, 10))
        self.pause_btn = ttk.Button(buttons, text="Pause",
                                    command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        self.close_btn = ttk.Button(buttons, text="Cancel", command=self._close)
        self.close_btn.pack(side="left", padx=4)

    # -- worker process -------------------------------------------------------
    def _start(self):
        fd, cfg_path = tempfile.mkstemp(prefix="filteragent_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f)
        self.proc = subprocess.Popen(
            # absolute: the worker runs from the install folder, not ours
            [_python(), "-m", "gui.worker", str(self.movie.resolve()), cfg_path],
            cwd=TOOL_DIR, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
        self.job = proctree.bind_to_this_process(int(self.proc._handle))
        threading.Thread(target=self._read_events, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_events(self):
        ended = False
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ended = ended or ev["kind"] in ("done", "error")
            self.q.put((ev["kind"], ev["payload"]))
        code = self.proc.wait()
        if not ended and not self.cancelled:
            self.q.put(("error", f"The worker stopped unexpectedly (exit code "
                                 f"{code}).\n" + "".join(self.stderr_tail)))

    def _read_stderr(self):
        # must keep draining, or a chatty library fills the pipe and the
        # worker blocks; the tail is shown if the worker dies without a word
        for line in self.proc.stderr:
            self.stderr_tail.append(line.decode("utf-8", "replace"))

    # -- buttons --------------------------------------------------------------
    def _toggle_pause(self):
        if self.finished or not self.proc or self.proc.poll() is not None:
            return
        if self.suspended is None:
            self.suspended = proctree.suspend_tree(self.proc.pid)
            self.pause_btn.config(text="Resume")
            self._append("--- Paused (no CPU in use; Resume continues where it stopped) ---")
        else:
            proctree.resume_tree(self.suspended)
            self.suspended = None
            self.pause_btn.config(text="Pause")
            self._append("--- Resumed ---")
        self._show_stage()

    def _close(self):
        if self.proc and self.proc.poll() is None:
            self.cancelled = True
            proctree.kill_tree(self.proc.pid)
        self.root.destroy()

    # -- UI pump --------------------------------------------------------------
    def _show_stage(self, text=None):
        if text is not None:
            self.stage_text = text
        self.stage_lbl.config(text=("Paused — " if self.suspended is not None else "")
                              + self.stage_text)

    def _finish(self, label):
        self.finished = True
        if self.suspended:  # paused in the instant the worker was finishing
            proctree.resume_tree(self.suspended)
        self.suspended = None
        self._show_stage(label)
        self.pause_btn.config(state="disabled")
        self.close_btn.config(text="Close")

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "stage":
                    self.bar["value"] = payload
                    self._show_stage(f"[{payload + 1}/{len(STAGES)}] {STAGES[payload]}")
                    self._append(f"--- {STAGES[payload]} ---")
                elif kind == "frameprog":
                    self._show_stage(f"[4/{len(STAGES)}] Sexual content — "
                                     f"{payload:.0%} of frames")
                elif kind == "done":
                    edl_path, sheet, srt_path = payload
                    self.bar["value"] = len(STAGES)
                    self._finish("Done")
                    self._append(f"\nEDL written: {edl_path}")
                    if srt_path:
                        self._append(f"Censored subtitles: {srt_path}")
                    self._append(f"Review sheet: {sheet}")
                    if sheet and self.cfg["review"].get("open_after", True):
                        webbrowser.open(Path(sheet).as_uri())
                    notify("FilterAgent", f"{self.movie.name} — filter EDL ready.")
                elif kind == "error":
                    self._finish("Error")
                    self._append(payload)
                    notify("FilterAgent", f"{self.movie.name} — processing failed.")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _append(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def run(self):
        try:
            self._start()
        except Exception as e:
            self._finish("Error")
            self._append(f"Couldn't start the worker process: {e}")
        self._pump()
        self.root.mainloop()
