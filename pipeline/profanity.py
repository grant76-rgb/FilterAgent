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


def _phrase_pattern(phrase: str) -> re.Pattern:
    """"hell's kitchen" -> matches Hell's Kitchen / hell’s  kitchen, any case."""
    parts = [re.escape(p).replace("'", "['’]") for p in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


def load_wordlist(path: Path, categories: dict | None = None) -> list:
    """Returns [(category, compiled_pattern), ...].

    Lines are tagged with the category from the most recent
    "# category: NAME" marker (default "profanity" before the first one).
    If `categories` is given, entries whose category maps to False are
    skipped; a category absent from the dict is treated as enabled.
    Lines starting with "!" are exceptions (see load_exceptions), not words.
    """
    patterns = []
    category = "profanity"
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("!"):
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


def load_exceptions(path: Path) -> list:
    """"!phrase" lines: phrases that are never muted or censored even though
    they contain a wordlist word — e.g. "!hell's kitchen" (a place name)."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw.startswith("!") and raw[1:].strip():
            out.append(_phrase_pattern(raw[1:].strip().lower()))
    return out


def exception_spans(text: str, exceptions) -> list:
    return [m.span() for ex in exceptions or () for m in ex.finditer(text)]


def _inside(span, spans) -> bool:
    return any(s <= span[0] and span[1] <= e for s, e in spans)


def word_matches(word: str, patterns: list) -> bool:
    """Does a single transcribed word match the wordlist? Whisper hands back
    words like "hell's", "fuckin'", "bad-ass": try the word as-is, without
    its possessive/contraction ending, and each hyphenated part."""
    w = re.sub(r"[^\w'\-]", "", word.lower().replace("’", "'"))
    cands = {w, w.replace("-", "")}
    cands.update(w.split("-"))
    for c in list(cands):
        cands.add(c.strip("'"))
        cands.add(re.sub(r"'\w{0,2}$", "", c))       # hell's -> hell, fuckin' -> fuckin
    cands.discard("")
    return any(p.fullmatch(c) for c in cands for _cat, p in patterns)


def censor_text(text: str, patterns: list, exceptions=()) -> str:
    """Replace every wordlist match in `text` with asterisks of the same
    length, except inside exception phrases. Used for both the exported
    .srt and the review sheet."""
    keep = exception_spans(text, exceptions)       # same-length replacement keeps spans valid

    def repl(m):
        return m.group(0) if _inside(m.span(), keep) else "*" * len(m.group(0))
    for _category, pattern in patterns:
        text = pattern.sub(repl, text)
    return text


def has_hit(text: str, patterns: list, exceptions=()) -> bool:
    keep = exception_spans(text, exceptions)
    return any(not _inside(m.span(), keep) for _c, p in patterns for m in p.finditer(text))


def scan(assets: dict, cfg: dict, log=print) -> list:
    pcfg = cfg["profanity"]
    wordlist = load_wordlist(Path(cfg["_wordlist_path"]), pcfg.get("categories"))
    exceptions = load_exceptions(Path(cfg["_wordlist_path"]))
    log(f"Wordlist: {len(wordlist)} entries" + (f", {len(exceptions)} exception(s)" if exceptions else ""))
    pad_b, pad_a = pcfg["mute_pad_before"], pcfg["mute_pad_after"]

    if pcfg.get("use_whisper_alignment"):
        ranges = _scan_whisper(assets, wordlist, exceptions, pad_b, pad_a, log)
        ranges += _recheck(assets, cfg, wordlist, exceptions, ranges, log)
        return _refine_edges(assets, cfg, ranges, log)

    if assets["srt"] is None:
        wav = assets.get("wav")
        if pcfg.get("whisper_fallback", True) and wav and wav.exists():
            log("No subtitles available — falling back to Whisper "
                "transcription.")
            ranges = _scan_whisper(assets, wordlist, exceptions, pad_b, pad_a, log)
            return _refine_edges(assets, cfg, ranges, log)
        log("No subtitles available — skipping profanity pass.")
        return []

    ranges = []
    subs = pysrt.open(str(assets["srt"]), encoding="utf-8", error_handling=pysrt.ERROR_PASS)
    for cue in subs:
        text = re.sub(r"<[^>]+>", "", cue.text)
        if has_hit(text, wordlist, exceptions):
            ranges.append({
                "start": max(0, cue.start.ordinal / 1000 - pad_b),
                "end": cue.end.ordinal / 1000 + pad_a,
                "action": 1,
                "reason": f"profanity: {text.strip()[:60]!r}",
                "confidence": 1.0,
            })
    log(f"{len(ranges)} subtitle cues flagged for muting")
    return ranges


def _refine_edges(assets, cfg, ranges, log) -> list:
    """Whisper's word timestamps often end before the word does; stretch each
    word mute over the audible word (see edges.py)."""
    if not cfg["profanity"].get("refine_word_edges", True) or not ranges:
        return ranges
    from . import edges
    return edges.refine(assets["movie"], ranges, log=log)


def _recheck(assets, cfg, wordlist, exceptions, ranges, log) -> list:
    """Whisper and the subtitles are independent: re-listen, with a larger
    model, to subtitle lines whose wordlist words got no mute (see recheck.py)."""
    pcfg = cfg["profanity"]
    if not pcfg.get("recheck_with_subtitles", True) or assets.get("srt") is None:
        return []
    from . import recheck
    subs = pysrt.open(str(assets["srt"]), encoding="utf-8", error_handling=pysrt.ERROR_PASS)
    cues = [(c.start.ordinal / 1000, c.end.ordinal / 1000, c.text) for c in subs]
    try:
        return recheck.find_missed(assets["movie"], cues, wordlist, exceptions, ranges,
                                   model_name=pcfg.get("recheck_model", recheck.DEFAULT_MODEL),
                                   pad_b=pcfg["mute_pad_before"], pad_a=pcfg["mute_pad_after"], log=log)
    except ImportError:
        log("faster-whisper not installed — skipping the subtitle cross-check")
        return []


def _scan_whisper(assets, wordlist, exceptions, pad_b, pad_a, log) -> list:
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
        words = seg.words or []
        line, spans = "", []
        for word in words:
            t = word.word.strip()
            spans.append((len(line), len(line) + len(t)))
            line += t + " "
        keep = exception_spans(line, exceptions)
        for word, span in zip(words, spans):
            if _inside(span, keep) or not word_matches(word.word, wordlist):
                continue
            clean = re.sub(r"[^\w'\-]", "", word.word.lower())
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
