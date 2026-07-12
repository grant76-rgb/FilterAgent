"""Censored-subtitle pass: replace wordlist matches with asterisks and write
an external .srt next to the movie. Runs after the EDL so it reuses the
already-extracted subs; never touches the source file or embedded tracks."""

from pathlib import Path

import pysrt

from .profanity import censor_text, load_wordlist


def censor(assets: dict, movie: Path, cfg: dict, log=print) -> Path | None:
    pcfg = cfg["profanity"]
    if not pcfg.get("censor_subtitles", True):
        return None
    if assets.get("srt") is None:
        log("No subtitles available — skipping censored subtitle export.")
        return None

    patterns = load_wordlist(Path(cfg["_wordlist_path"]), pcfg.get("categories"))
    if not patterns:
        log("Wordlist empty — skipping censored subtitle export.")
        return None

    subs = pysrt.open(str(assets["srt"]), error_handling=pysrt.ERROR_PASS)
    changed = 0
    for cue in subs:
        censored = censor_text(cue.text, patterns)
        if censored != cue.text:
            changed += 1
        cue.text = censored

    out_path = movie.parent / f"{movie.stem}.en.srt"
    subs.save(str(out_path), encoding="utf-8")
    log(f"Censored subtitles: {out_path.name} ({changed} lines changed)")
    return out_path
