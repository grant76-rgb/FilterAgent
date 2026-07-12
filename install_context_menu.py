#!/usr/bin/env python3
"""
Install / uninstall the Explorer right-click menu for .mkv files.

    python install_context_menu.py            # install
    python install_context_menu.py --remove   # uninstall

Writes to HKEY_CURRENT_USER (no admin rights needed):

  HKCU\\Software\\Classes\\SystemFileAssociations\\.mkv\\shell\\FilterAgent
      (Default) = "Create filter EDL"
      Icon      = <pythonw.exe>
  ...\\FilterAgent\\command
      (Default) = "<pythonw.exe>" "<this folder>\\app.py" "%1"

Also adds:
  - "FilterAgentProfanityOnly" -> "Create filter EDL (profanity only)",
    which skips scene detection and sexual-content classification for that
    run (faster; good for TV where nudity filtering matters less).
  - "FilterAgentSettings" -> "FilterAgent settings", reachable from any
    .mkv right-click.

Uses pythonw.exe (not python.exe) so no console window flashes behind the GUI.
"""

import argparse
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    sys.exit("This installer only runs on Windows.")

BASE = r"Software\Classes\SystemFileAssociations\.mkv\shell"
APP_DIR = Path(__file__).parent.resolve()


def pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.parent / "pythonw.exe"
    return str(w if w.exists() else exe)


def install():
    py = pythonw()
    app = APP_DIR / "app.py"

    entries = {
        "FilterAgent": {
            "label": "Create filter EDL",
            "command": f'"{py}" "{app}" "%1"',
        },
        "FilterAgentProfanityOnly": {
            "label": "Create filter EDL (profanity only)",
            "command": f'"{py}" "{app}" "%1" --profanity-only',
        },
        "FilterAgentSettings": {
            "label": "FilterAgent settings",
            "command": f'"{py}" "{app}"',
        },
    }
    for key, spec in entries.items():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{BASE}\{key}") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, spec["label"])
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, py)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"{BASE}\{key}\command") as k:
            winreg.SetValue(k, "", winreg.REG_SZ, spec["command"])

    print("Installed. Right-click any .mkv -> 'Create filter EDL'.")
    print(f"  command: {entries['FilterAgent']['command']}")


def remove():
    for key in ("FilterAgent", "FilterAgentProfanityOnly", "FilterAgentSettings"):
        for sub in (rf"{BASE}\{key}\command", rf"{BASE}\{key}"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except FileNotFoundError:
                pass
    print("Removed context menu entries.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()
    remove() if args.remove else install()
