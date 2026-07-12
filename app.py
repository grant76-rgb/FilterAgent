#!/usr/bin/env python3
"""
FilterAgent for Windows

Three ways to launch:
  app.py                     -> opens the Settings window
  app.py "C:\\path\\Movie.mkv" -> opens the Progress window and processes the
                                file, writing Movie.edl into the same folder
  app.py "C:\\path\\Movie.mkv" --profanity-only
                              -> same, but skips scene detection and sexual-
                                 content classification for this run only
                                 (saved settings are untouched)

The Explorer right-click entries (installed by install_context_menu.py) call
these forms with the selected file.

User config lives in %APPDATA%\\FilterAgent\\  (config.yaml + wordlist.txt),
bootstrapped from defaults/ on first run.
"""

import argparse
import copy
import os
import shutil
from pathlib import Path

import yaml

APP_NAME = "FilterAgent"


def config_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def bootstrap_config() -> Path:
    """Copy default config + wordlist to APPDATA on first run."""
    d = config_dir()
    defaults = Path(__file__).parent / "defaults"
    cfg = d / "config.yaml"
    wl = d / "wordlist.txt"
    if not cfg.exists():
        shutil.copy(defaults / "config.yaml", cfg)
    if not wl.exists():
        shutil.copy(defaults / "wordlist.txt", wl)
    return cfg


def _merge_defaults(cfg: dict, defaults: dict) -> bool:
    """Fill in keys missing from an existing user config (e.g. after an
    upgrade adds new settings). Returns True if anything was added."""
    changed = False
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            changed = _merge_defaults(cfg[k], v) or changed
    return changed


def load_config() -> dict:
    cfg_path = bootstrap_config()
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    defaults = yaml.safe_load(
        (Path(__file__).parent / "defaults" / "config.yaml").read_text(encoding="utf-8"))
    if _merge_defaults(cfg, defaults):
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    cfg["_config_path"] = str(cfg_path)
    cfg["_wordlist_path"] = str(config_dir() / "wordlist.txt")
    return cfg


def save_config(cfg: dict):
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    (config_dir() / "config.yaml").write_text(
        yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def main():
    cfg = load_config()

    ap = argparse.ArgumentParser()
    ap.add_argument("movie", nargs="?")
    ap.add_argument("--profanity-only", action="store_true",
                    help="Skip scene detection and sexual-content "
                         "classification for this run only.")
    args = ap.parse_args()

    if args.movie:
        movie = Path(args.movie)
        if args.profanity_only:
            cfg = copy.deepcopy(cfg)
            cfg["nsfw"]["enabled"] = False
        from gui.progress import ProgressWindow
        ProgressWindow(movie, cfg).run()
    else:
        from gui.settings import SettingsWindow
        SettingsWindow(cfg).run()


if __name__ == "__main__":
    main()
