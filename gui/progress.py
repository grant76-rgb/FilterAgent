"""Progress window: runs the pipeline in a worker thread, streams log lines,
writes the EDL next to the movie, optionally opens the review sheet."""

import queue
import threading
import traceback
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from gui.notify import notify

STAGES = ["Extracting", "Profanity", "Scenes", "Sexual content", "Writing EDL",
          "Censoring subtitles", "Cleaning up"]


class ProgressWindow:
    def __init__(self, movie: Path, cfg: dict):
        self.movie = movie
        self.cfg = cfg
        self.q = queue.Queue()

        self.root = tk.Tk()
        self.root.title(f"FilterAgent — {movie.name}")
        self.root.geometry("560x380")

        self.stage_lbl = ttk.Label(self.root, text="Starting...",
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

        self.close_btn = ttk.Button(self.root, text="Cancel",
                                    command=self.root.destroy)
        self.close_btn.pack(pady=(0, 10))

    # -- worker ---------------------------------------------------------------
    def _emit(self, kind, payload):
        self.q.put((kind, payload))

    def _worker(self):
        try:
            from pipeline import extract, profanity, scenes, nsfw, edl, subtitles, review, cleanup

            workdir = self.movie.parent / ".filteragent" / self.movie.stem
            workdir.mkdir(parents=True, exist_ok=True)

            self._emit("stage", 0)
            assets = extract.run(self.movie, workdir, self.cfg,
                                 log=lambda m: self._emit("log", m))

            ranges = []
            self._emit("stage", 1)
            if self.cfg["profanity"]["enabled"]:
                ranges += profanity.scan(assets, self.cfg,
                                         log=lambda m: self._emit("log", m))
            else:
                self._emit("log", "Profanity muting disabled in settings.")

            self._emit("stage", 2)
            if self.cfg["nsfw"]["enabled"]:
                scene_list = scenes.detect(self.movie, workdir, self.cfg,
                                           log=lambda m: self._emit("log", m))
            else:
                scene_list = []
                self._emit("log", "Sexual content filter disabled — "
                                  "skipping scene detection.")

            self._emit("stage", 3)
            if self.cfg["nsfw"]["enabled"]:
                ranges += nsfw.scan(assets, scene_list, self.cfg,
                                    log=lambda m: self._emit("log", m),
                                    progress=lambda p: self._emit("frameprog", p))
            else:
                self._emit("log", "Sexual content filter disabled in settings.")

            self._emit("stage", 4)
            edl_path = edl.write(self.movie, ranges, workdir, self.cfg,
                                 log=lambda m: self._emit("log", m))

            self._emit("stage", 5)
            srt_path = subtitles.censor(assets, self.movie, self.cfg,
                                        log=lambda m: self._emit("log", m))

            sheet = review.build_contact_sheet(workdir, self.movie, self.cfg)

            self._emit("stage", 6)
            sheet = cleanup.finalize(
                self.movie, workdir, sheet,
                delete_workdir=self.cfg["cleanup"].get("delete_workdir", True),
                log=lambda m: self._emit("log", m))

            self._emit("done", (edl_path, sheet, srt_path))
        except Exception:
            self._emit("error", traceback.format_exc())

    # -- UI pump --------------------------------------------------------------
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "stage":
                    self.bar["value"] = payload
                    self.stage_lbl.config(
                        text=f"[{payload + 1}/{len(STAGES)}] {STAGES[payload]}")
                    self._append(f"--- {STAGES[payload]} ---")
                elif kind == "frameprog":
                    self.stage_lbl.config(
                        text=f"[4/{len(STAGES)}] Sexual content — "
                             f"{payload:.0%} of frames")
                elif kind == "done":
                    edl_path, sheet, srt_path = payload
                    self.bar["value"] = len(STAGES)
                    self.stage_lbl.config(text="Done")
                    self._append(f"\nEDL written: {edl_path}")
                    if srt_path:
                        self._append(f"Censored subtitles: {srt_path}")
                    self._append(f"Review sheet: {sheet}")
                    self.close_btn.config(text="Close")
                    if sheet and self.cfg["review"].get("open_after", True):
                        webbrowser.open(Path(sheet).as_uri())
                    notify("FilterAgent", f"{self.movie.name} — filter EDL ready.")
                elif kind == "error":
                    self.stage_lbl.config(text="Error")
                    self._append(payload)
                    self.close_btn.config(text="Close")
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
        threading.Thread(target=self._worker, daemon=True).start()
        self._pump()
        self.root.mainloop()
