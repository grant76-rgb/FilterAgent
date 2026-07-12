"""Profanity pass: wordlist matching against subtitles -> MUTE ranges."""

import re
from pathlib import Path

import pysrt


_CATEGORY_RE = re.compile(r"#\s*category:\s*(\S+)", re.IGNORECASE)


def _pattern_for(word: str) -> re.Pattern:
    """"damn*" -> \\bdamn\\w*\\b. Wildcard stays inside the word — unlike
    fnmatch.translate's ".*", it won't swallow the rest of the line."""
    parts = [re.escape(p) for p in word.split("*")]
    body = r"\w*".join(parts)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


def load_wordlist(path: Path, categories: dict | None = None) -> list:
    """Returns [(category, compiled_pattern), ...].

    Lines are tagged with the category from the most recent
    "# category: NAME" marker (default "profanity" before the first one).
    If `categories` is given, entries whose category maps to False are
    skipped; a category absent from the dict is treated as enabled.
    """
    patterns = []
    category = "profanity"
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#"):
            m = _CATEGORY_RE.match(raw)
            if m:
                category = m.group(1).lower()
            continue
        if categories is not None and not categories.get(category, True):
            continue
        patterns.append((category, _pattern_for(raw.lower())))
    return patterns


def censor_text(text: str, patterns: list) -> str:
    """Replace every wordlist match in `text` with asterisks of the same
    length. Used for both the exported .srt and the review sheet."""
    for _category, pattern in patterns:
        text = pattern.sub(lambda m: "*" * len(m.group(0)), text)
    return text


def scan(assets: dict, cfg: dict, log=print) -> list:
    pcfg = cfg["profanity"]
    wordlist = load_wordlist(Path(cfg["_wordlist_path"]), pcfg.get("categories"))
    log(f"Wordlist: {len(wordlist)} entries")
    pad_b, pad_a = pcfg["mute_pad_before"], pcfg["mute_pad_after"]

    if pcfg.get("use_whisper_alignment"):
        return _scan_whisper(assets, wordlist, pad_b, pad_a, log)

    if assets["srt"] is None:
        wav = assets.get("wav")
        if pcfg.get("whisper_fallback", True) and wav and wav.exists():
            log("No subtitles available — falling back to Whisper "
                "transcription.")
            return _scan_whisper(assets, wordlist, pad_b, pad_a, log)
        log("No subtitles available — skipping profanity pass.")
        return []

    ranges = []
    subs = pysrt.open(str(assets["srt"]), error_handling=pysrt.ERROR_PASS)
    for cue in subs:
        text = re.sub(r"<[^>]+>", "", cue.text)
        if any(p.search(text) for _cat, p in wordlist):
            ranges.append({
                "start": max(0, cue.start.ordinal / 1000 - pad_b),
                "end": cue.end.ordinal / 1000 + pad_a,
                "action": 1,
                "reason": f"profanity: {text.strip()[:60]!r}",
                "confidence": 1.0,
            })
    log(f"{len(ranges)} subtitle cues flagged for muting")
    return ranges


def _scan_whisper(assets, wordlist, pad_b, pad_a, log) -> list:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper not installed — skipping profanity pass "
            "(pip install faster-whisper)")
        return []

    log("Transcribing with faster-whisper (word timestamps)...")
    log("(this has no live progress bar — CPU transcription of a full "
        "movie/episode audio track routinely takes 10-30+ min; the "
        "lines below confirm it's still moving, not stuck)")
    model = WhisperModel("small.en", compute_type="int8")
    segments, info = model.transcribe(str(assets["wav"]), word_timestamps=True)
    duration = assets.get("duration") or getattr(info, "duration", None)

    ranges = []
    srt_cues = []
    next_pct = 10
    for seg in segments:
        if duration:
            pct = seg.end / duration * 100
            if pct >= next_pct:
                log(f"  ...transcribed {pct:.0f}% "
                    f"({seg.end/60:.1f} / {duration/60:.1f} min)")
                next_pct = (int(pct) // 10 + 1) * 10
        text = seg.text.strip()
        if text:
            srt_cues.append((seg.start, seg.end, text))
        for word in seg.words or []:
            clean = re.sub(r"[^\w']", "", word.word.lower())
            if any(p.fullmatch(clean) for _cat, p in wordlist):
                ranges.append({
                    "start": max(0, word.start - pad_b),
                    "end": word.end + pad_a,
                    "action": 1,
                    "reason": f"profanity(word): {clean}",
                    "confidence": word.probability,
                })
    log(f"{len(ranges)} words flagged for muting")

    # If there was no real subtitle to begin with, the transcript we just
    # produced *is* the subtitle now: save it so the censored-subtitle
    # export step (and any later run) has real cues to work with.
    if srt_cues and assets.get("srt") is None:
        assets["srt"] = _save_whisper_srt(srt_cues, assets, log)

    return ranges


def _save_whisper_srt(cues, assets, log) -> Path:
    subs = pysrt.SubRipFile()
    for i, (start, end, text) in enumerate(cues, start=1):
        subs.append(pysrt.SubRipItem(
            index=i,
            start=pysrt.SubRipTime(milliseconds=int(round(start * 1000))),
            end=pysrt.SubRipTime(milliseconds=int(round(end * 1000))),
            text=text,
        ))

    workdir_path = assets["workdir"] / "subs.srt"
    subs.save(str(workdir_path), encoding="utf-8")

    # Also drop a copy next to the movie, named to match it exactly, so
    # players/subsequent runs pick it up like any other external subtitle.
    external_path = assets["movie"].with_suffix(".srt")
    subs.save(str(external_path), encoding="utf-8")
    log(f"Whisper transcript saved as subtitles: {external_path.name}")

    return workdir_path
