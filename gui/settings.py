"""Settings window: wordlist editor + NSFW sensitivity + general options.

Plain tkinter/ttk — no extra GUI dependencies to install or package.
"""

import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

PRESETS = {
    # label: (enabled, threshold) overrides applied on top of config
    "strict": {
        "FEMALE_BREAST_EXPOSED":    (True, 0.45),
        "FEMALE_GENITALIA_EXPOSED": (True, 0.35),
        "MALE_GENITALIA_EXPOSED":   (True, 0.35),
        "BUTTOCKS_EXPOSED":         (True, 0.50),
        "ANUS_EXPOSED":             (True, 0.35),
        "FEMALE_BREAST_COVERED":    (True, 0.60),
        "BUTTOCKS_COVERED":         (True, 0.65),
        "MALE_BREAST_EXPOSED":      (False, 0.80),
        "BELLY_EXPOSED":            (False, 0.85),
    },
    "moderate": {
        "FEMALE_BREAST_EXPOSED":    (True, 0.60),
        "FEMALE_GENITALIA_EXPOSED": (True, 0.50),
        "MALE_GENITALIA_EXPOSED":   (True, 0.50),
        "BUTTOCKS_EXPOSED":         (True, 0.65),
        "ANUS_EXPOSED":             (True, 0.50),
        "FEMALE_BREAST_COVERED":    (False, 0.75),
        "BUTTOCKS_COVERED":         (False, 0.75),
        "MALE_BREAST_EXPOSED":      (False, 0.80),
        "BELLY_EXPOSED":            (False, 0.85),
    },
}

FRIENDLY = {
    "FEMALE_BREAST_EXPOSED": "Nudity — breast",
    "FEMALE_GENITALIA_EXPOSED": "Nudity — genitalia (F)",
    "MALE_GENITALIA_EXPOSED": "Nudity — genitalia (M)",
    "BUTTOCKS_EXPOSED": "Nudity — buttocks",
    "ANUS_EXPOSED": "Nudity — explicit",
    "FEMALE_BREAST_COVERED": "Suggestive — lingerie/covered (F)",
    "BUTTOCKS_COVERED": "Suggestive — covered buttocks",
    "MALE_BREAST_EXPOSED": "Shirtless (M)",
    "BELLY_EXPOSED": "Bare midriff",
}


class SettingsWindow:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = tk.Tk()
        self.root.title("FilterAgent — Settings")
        self.root.minsize(640, 400)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_wordlist_tab(nb)
        self._build_nsfw_tab(nb)
        self._build_general_tab(nb)

        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="Save", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.root.destroy).pack(
            side="right", padx=6)

        # Size to fit the tallest tab (Notebook sizes to its largest pane)
        # instead of a hardcoded guess, capped so it can't grow off-screen.
        self.root.update_idletasks()
        w = self.root.winfo_reqwidth()
        h = min(self.root.winfo_reqheight(), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{w}x{h}")

    # --- Tab 1: wordlist -------------------------------------------------
    def _build_wordlist_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Wordlist")

        self.prof_enabled = tk.BooleanVar(
            value=self.cfg["profanity"]["enabled"])
        ttk.Checkbutton(tab, text="Mute profanity",
                        variable=self.prof_enabled).pack(anchor="w", padx=8, pady=6)

        categories = self.cfg["profanity"].setdefault(
            "categories", {"profanity": True, "religious_identity": False})
        self.cat_religious = tk.BooleanVar(
            value=categories.get("religious_identity", False))
        ttk.Checkbutton(
            tab, text="Also mute religious & identity terms (god, gay, jew, "
                      "queer, ...) — off by default, see wordlist below",
            variable=self.cat_religious).pack(anchor="w", padx=8, pady=(0, 6))

        self.censor_subs = tk.BooleanVar(
            value=self.cfg["profanity"].get("censor_subtitles", True))
        ttk.Checkbutton(
            tab, text="Write censored subtitles (banned words -> ***) as an "
                      "external .srt next to the movie",
            variable=self.censor_subs).pack(anchor="w", padx=8, pady=(0, 6))

        ttk.Label(tab, text="One word or phrase per line. * = wildcard "
                            "(damn* matches damned, dammit...) Use "
                            "\"# category: NAME\" to start a new section.").pack(
            anchor="w", padx=8)

        frame = ttk.Frame(tab)
        frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.wordbox = tk.Text(frame, wrap="none", undo=True)
        ys = ttk.Scrollbar(frame, command=self.wordbox.yview)
        self.wordbox.configure(yscrollcommand=ys.set)
        self.wordbox.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

        wl = Path(self.cfg["_wordlist_path"])
        if wl.exists():
            self.wordbox.insert("1.0", wl.read_text(encoding="utf-8"))

        pads = ttk.Frame(tab)
        pads.pack(fill="x", padx=8, pady=4)
        ttk.Label(pads, text="Mute padding (sec):  before").pack(side="left")
        self.pad_b = tk.DoubleVar(value=self.cfg["profanity"]["mute_pad_before"])
        ttk.Spinbox(pads, from_=0, to=2, increment=0.05, width=5,
                    textvariable=self.pad_b).pack(side="left", padx=4)
        ttk.Label(pads, text="after").pack(side="left")
        self.pad_a = tk.DoubleVar(value=self.cfg["profanity"]["mute_pad_after"])
        ttk.Spinbox(pads, from_=0, to=2, increment=0.05, width=5,
                    textvariable=self.pad_a).pack(side="left", padx=4)

        self.whisper = tk.BooleanVar(
            value=self.cfg["profanity"].get("use_whisper_alignment", False))
        ttk.Checkbutton(tab, text="Word-exact mutes via Whisper (slower; "
                                  "requires faster-whisper installed)",
                        variable=self.whisper).pack(anchor="w", padx=8, pady=4)

        self.whisper_fallback = tk.BooleanVar(
            value=self.cfg["profanity"].get("whisper_fallback", True))
        ttk.Checkbutton(tab, text="If no subtitles can be found at all "
                                  "(embedded, external, or downloaded), "
                                  "transcribe with Whisper instead of "
                                  "skipping the profanity pass",
                        variable=self.whisper_fallback).pack(
            anchor="w", padx=8, pady=4)

    # --- Tab 2: sexual content filter ------------------------------------
    def _build_nsfw_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="Sexual content")

        self.nsfw_enabled = tk.BooleanVar(value=self.cfg["nsfw"]["enabled"])
        ttk.Checkbutton(tab, text="Skip scenes with sexual content",
                        variable=self.nsfw_enabled).pack(anchor="w", padx=8, pady=6)

        pf = ttk.Frame(tab)
        pf.pack(anchor="w", padx=8)
        ttk.Label(pf, text="Preset:").pack(side="left")
        self.preset = tk.StringVar(value=self.cfg["nsfw"].get("preset", "moderate"))
        for name in ("strict", "moderate", "custom"):
            ttk.Radiobutton(pf, text=name.title(), value=name,
                            variable=self.preset,
                            command=self._apply_preset).pack(side="left", padx=6)

        ttk.Label(tab, text="Category                                  "
                            "on      sensitivity (higher = more sensitive)"
                  ).pack(anchor="w", padx=8, pady=(10, 0))

        self.label_vars = {}
        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=8)
        for row, (label, spec) in enumerate(self.cfg["nsfw"]["labels"].items()):
            en = tk.BooleanVar(value=spec["enabled"])
            # invert threshold for the slider so right = more sensitive
            sens = tk.DoubleVar(value=round(1 - spec["threshold"], 2))
            self.label_vars[label] = (en, sens)

            cb = ttk.Checkbutton(grid, text=FRIENDLY.get(label, label),
                                 variable=en, command=self._set_custom)
            cb.grid(row=row, column=0, sticky="w", pady=2)
            sc = ttk.Scale(grid, from_=0.1, to=0.9, variable=sens,
                           length=220, command=lambda *_: self._set_custom())
            sc.grid(row=row, column=1, padx=10)
        grid.columnconfigure(0, minsize=280)

        ttk.Label(tab, text="Skip whole scene when this fraction of its "
                            "frames are flagged:").pack(anchor="w", padx=8,
                                                        pady=(12, 0))
        self.scene_ratio = tk.DoubleVar(value=self.cfg["nsfw"]["scene_skip_ratio"])
        ttk.Scale(tab, from_=0.1, to=0.9, variable=self.scene_ratio,
                  length=220).pack(anchor="w", padx=8)

        pad = ttk.Frame(tab)
        pad.pack(anchor="w", padx=8, pady=(12, 0))
        ttk.Label(pad, text="Extra padding before/after a flagged moment "
                            "(sec) — raise this if skips start late on "
                            "content that exposes gradually:").pack(anchor="w")
        self.span_pad = tk.DoubleVar(value=self.cfg["nsfw"].get("span_pad", 2.0))
        ttk.Spinbox(pad, from_=0, to=15, increment=0.5, width=5,
                    textvariable=self.span_pad).pack(anchor="w", pady=2)

    def _apply_preset(self):
        name = self.preset.get()
        if name == "custom":
            return
        for label, (enabled, thr) in PRESETS[name].items():
            if label in self.label_vars:
                en, sens = self.label_vars[label]
                en.set(enabled)
                sens.set(round(1 - thr, 2))

    def _set_custom(self):
        self.preset.set("custom")

    # --- Tab 3: general ----------------------------------------------------
    def _build_general_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="General")

        f = ttk.Frame(tab)
        f.pack(anchor="w", padx=8, pady=8)
        ttk.Label(f, text="Frames sampled per second (higher = more "
                          "thorough, slower):").grid(row=0, column=0, sticky="w")
        self.fps = tk.DoubleVar(value=self.cfg["sampling"]["fps"])
        ttk.Spinbox(f, from_=0.5, to=4, increment=0.5, width=5,
                    textvariable=self.fps).grid(row=0, column=1, padx=6)

        self.notify = tk.BooleanVar(
            value=self.cfg["edl"].get("skip_action", 0) == 3)
        ttk.Checkbutton(tab, text='Show "skipped" notification in Kodi '
                                  "(EDL action 3 instead of silent cut)",
                        variable=self.notify).pack(anchor="w", padx=8, pady=4)

        self.open_review = tk.BooleanVar(
            value=self.cfg["review"].get("open_after", True))
        ttk.Checkbutton(tab, text="Open review sheet in browser when "
                                  "processing finishes",
                        variable=self.open_review).pack(anchor="w", padx=8, pady=4)

        self.show_frames = tk.BooleanVar(
            value=self.cfg["review"].get("show_frames", True))
        ttk.Checkbutton(tab, text="Show the triggered frame thumbnail in "
                                  "the review page by default (can also be "
                                  "toggled per-page)",
                        variable=self.show_frames).pack(
            anchor="w", padx=8, pady=4)

        subs_cfg = self.cfg.get("subtitles", {})
        self.auto_dl = tk.BooleanVar(
            value=subs_cfg.get("auto_download", True))
        ttk.Checkbutton(tab, text="Auto-download subtitles online (via "
                                  "subliminal) when none are embedded or "
                                  "found next to the movie",
                        variable=self.auto_dl).pack(anchor="w", padx=8, pady=4)

        self.cleanup_workdir = tk.BooleanVar(
            value=self.cfg.get("cleanup", {}).get("delete_workdir", True))
        ttk.Checkbutton(tab, text="Delete working files after each run "
                                  "(frames, cached scene/analysis data) — "
                                  "turn off to speed up re-runs while "
                                  "tuning settings",
                        variable=self.cleanup_workdir).pack(
            anchor="w", padx=8, pady=4)

        ttk.Label(tab, text=f"Config folder: {Path(self.cfg['_config_path']).parent}",
                  foreground="#666").pack(anchor="w", padx=8, pady=(20, 0))

    # --- Save ----------------------------------------------------------------
    def save(self):
        from app import save_config
        c = self.cfg
        c["profanity"]["enabled"] = self.prof_enabled.get()
        c["profanity"]["mute_pad_before"] = round(self.pad_b.get(), 2)
        c["profanity"]["mute_pad_after"] = round(self.pad_a.get(), 2)
        c["profanity"]["use_whisper_alignment"] = self.whisper.get()
        c["profanity"]["whisper_fallback"] = self.whisper_fallback.get()
        c["profanity"]["categories"] = {
            "profanity": True,
            "religious_identity": self.cat_religious.get(),
        }
        c["profanity"]["censor_subtitles"] = self.censor_subs.get()

        c["nsfw"]["enabled"] = self.nsfw_enabled.get()
        c["nsfw"]["preset"] = self.preset.get()
        for label, (en, sens) in self.label_vars.items():
            c["nsfw"]["labels"][label] = {
                "enabled": en.get(),
                "threshold": round(1 - sens.get(), 2),
            }
        c["nsfw"]["scene_skip_ratio"] = round(self.scene_ratio.get(), 2)
        c["nsfw"]["span_pad"] = round(self.span_pad.get(), 2)

        c["sampling"]["fps"] = self.fps.get()
        c["edl"]["skip_action"] = 3 if self.notify.get() else 0
        c["review"]["open_after"] = self.open_review.get()
        c["review"]["show_frames"] = self.show_frames.get()
        c.setdefault("cleanup", {})["delete_workdir"] = self.cleanup_workdir.get()
        c.setdefault("subtitles", {})["auto_download"] = self.auto_dl.get()

        Path(c["_wordlist_path"]).write_text(
            self.wordbox.get("1.0", "end").strip() + "\n", encoding="utf-8")
        save_config(c)
        messagebox.showinfo("FilterAgent", "Settings saved.")
        self.root.destroy()

    def run(self):
        self.root.mainloop()
