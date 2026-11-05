"""Second opinion on the Whisper pass, using the subtitles.

Whisper (small.en, whole episode) and the subtitles are independent sources.
Wherever the synced subtitles have a wordlist word — or an already-censored
"****" — but the line got fewer mutes than it has such words, that line's
audio is transcribed again with a larger, more accurate model, and each
missing word is located as precisely as possible:

  1. heard   the larger model hears a wordlist word -> mute exactly that word
  2. aligned otherwise the subtitle text is lined up with what the model heard;
             the word sitting where the subtitle has the swear is muted, or,
             if the model skipped it, the gap between its neighbours
  3. line    nothing lines up (music, crosstalk): the whole subtitle line is
             muted, marked low-confidence so it sorts first on the review sheet

Lines whose audio isn't English (subtitled Russian/Spanish dialogue) are
handled differently: the subtitle's swear is a translation, so it can't be
lined up with the speech. Only words in what's actually said that are on the
wordlist get muted (entries can be in any language, e.g. сука or mierda);
otherwise the audio is left alone. Each such line is logged.

On Daredevil S1 the main pass missed ~35 such words in 13 episodes.
"""

import os
import re
import subprocess
from difflib import SequenceMatcher

SR = 16000
WINDOW_PAD = 1.0       # seconds of audio either side of the subtitle line
LINE_SLACK = 0.3       # a mute this close to a line counts as belonging to it
MAX_SPAN = 2.0         # an aligned mute longer than this means the alignment is off
MIN_GAP_MUTE = 0.3     # a skipped word gets at least this much around its gap
DEFAULT_MODEL = "large-v3"

CREATE_NO_WINDOW = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0
# a word: letters/digits/asterisks, with inner apostrophes or hyphens — a
# subtitle's "- " dialogue dash is not a word (counting it made good
# alignments look unreliable)
_TOKEN = re.compile(r"[\w'’*\-]*[\w*][\w'’*\-]*")
MIN_FOREIGN_PROB = 0.6   # this sure it's one other language -> treat the line as foreign
MAX_EN_PROB = 0.25       # ...or English this unlikely (short clips often split the vote,
                         # e.g. es 0.35 / it 0.34 / en 0.13 for one Spanish line)


def _is_foreign(info) -> bool:
    if info.language == "en":
        return False
    p_en = dict(getattr(info, "all_language_probs", None) or []).get("en", 1.0)
    return info.language_probability >= MIN_FOREIGN_PROB or p_en < MAX_EN_PROB


def _norm(tok: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tok.lower())


def line_targets(text: str, patterns, exceptions):
    """Tokens of a subtitle line, and the indexes of the ones that should be
    muted: wordlist words outside exception phrases, or "****" tokens."""
    from .profanity import word_matches, exception_spans
    keep = exception_spans(text, exceptions)
    toks = list(_TOKEN.finditer(text))
    targets = [i for i, m in enumerate(toks)
               if not any(s <= m.start() and m.end() <= e for s, e in keep)
               and ("*" in m.group(0) or word_matches(m.group(0), patterns))]
    return [m.group(0) for m in toks], targets


def _audio(movie, start, dur):
    import numpy as np
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(movie), "-map", "0:a:0", "-ac", "1", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    return np.frombuffer(r.stdout, np.float32)


def _fmt(t):
    return f"{int(t // 60)}:{t % 60:05.2f}"


def find_missed(movie, cues, patterns, exceptions, existing, model_name=DEFAULT_MODEL,
                pad_b=0.0, pad_a=0.0, log=print, model=None) -> list:
    """cues: [(start, end, text)] on the video's timeline. existing: ranges
    already found. Returns the additional mute ranges."""
    from .profanity import word_matches, exception_spans
    mutes = [r for r in existing if r.get("action") == 1]
    todo = []
    for s, e, text in cues:
        text = re.sub(r"<[^>]+>", "", text).replace("\n", " ")
        toks, targets = line_targets(text, patterns, exceptions)
        if not targets:
            continue
        have = sum(1 for r in mutes if r["start"] < e + LINE_SLACK and r["end"] > s - LINE_SLACK)
        if have < len(targets):
            todo.append((s, e, text, toks, targets, have))
    if not todo:
        log("Subtitle cross-check: every flagged subtitle line already has its mute(s)")
        return []
    log(f"Subtitle cross-check: {len(todo)} subtitle line(s) have words the main pass didn't "
        f"mute — re-listening with {model_name}...")
    if model is None:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_name, compute_type="int8",
                             cpu_threads=max(1, (os.cpu_count() or 2) // 2))

    added = []

    def free(a, b):
        return not any(r["start"] < b and r["end"] > a for r in mutes + added)

    def add(a, b, conf, reason):
        if b - a < 0.05:
            mid = (a + b) / 2
            a, b = mid - 0.1, mid + 0.1
        if free(a, b):
            added.append({"start": max(0.0, a - pad_b), "end": b + pad_a, "action": 1,
                          "reason": reason, "confidence": conf})
            return True
        return False

    counts = {"heard": 0, "aligned": 0, "line": 0, "foreign": 0}
    opts = dict(beam_size=5, word_timestamps=True, condition_on_previous_text=False, vad_filter=False)

    def foreign(segs, lang, offset, s, text):
        """Subtitled foreign dialogue: the subtitle's swear is a translation,
        so positions don't line up. Mute only words in what's actually said
        that are on the wordlist (entries can be in any language); otherwise
        leave the audio alone. The subtitle stays censored either way."""
        counts["foreign"] += 1
        heard = " ".join(seg.text.strip() for seg in segs)
        hits = [(w.word.strip(), offset + w.start, offset + w.end, w.probability)
                for seg in segs for w in (seg.words or []) if word_matches(w.word, patterns)]
        muted = [wt for wt, a, b, p in hits if add(a, b, p, f"profanity(recheck,{lang}): {_norm(wt) or wt}")]
        counts["heard"] += len(muted)
        log(f"  {_fmt(s)} {lang} dialogue: heard {heard[:70]!r} — "
            + (f"muted {', '.join(muted)}" if muted else "nothing on the wordlist, not muted")
            + f"  [{text[:60]}]")
    for s, e, text, toks, targets, have in todo:
        need = len(targets) - have
        w0 = max(0.0, s - WINDOW_PAD)
        dur = (e - s) + 2 * WINDOW_PAD
        audio = _audio(movie, w0, dur)
        segs, info = model.transcribe(audio, language=None, **opts)
        segs = list(segs)
        if _is_foreign(info):
            foreign(segs, info.language, w0, s, text)
            continue
        if info.language != "en":
            segs = list(model.transcribe(audio, language="en", **opts)[0])
        words = [(w.word.strip(), w0 + w.start, w0 + w.end, w.probability)
                 for seg in segs for w in (seg.words or []) if w.word.strip()]

        # 1. the larger model hears the word itself
        heard_line = " ".join(w[0] for w in words)
        keep, pos = exception_spans(heard_line, exceptions), 0
        for wt, a, b, p in words:
            span = (pos, pos + len(wt))
            pos += len(wt) + 1
            if word_matches(wt, patterns) and not any(x <= span[0] and span[1] <= y for x, y in keep):
                if add(a, b, p, f"profanity(recheck): {_norm(wt)}"):
                    counts["heard"] += 1
                    need -= 1
                    log(f"  {_fmt(a)} heard {wt!r}  [{text[:70]}]")
        if need <= 0:
            continue

        # 2. line the subtitle up with what was heard; anything still unmuted
        #    at a swear's position gets muted there
        cue_norm = [f"\x00{i}" if i in targets else _norm(t) for i, t in enumerate(toks)]
        heard_norm = [_norm(w[0]) for w in words]
        sm = SequenceMatcher(None, cue_norm, heard_norm, autojunk=False)
        others = len(toks) - len(targets)
        matched = sum(b.size for b in sm.get_matching_blocks())
        reliable = bool(words) and (others == 0 or matched / others >= 0.5)
        unresolved = []
        if reliable:
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                for i in range(i1, i2):
                    if i not in targets:
                        continue
                    if tag == "replace":
                        if i2 - i1 == j2 - j1:
                            a, b = words[j1 + i - i1][1:3]
                        else:
                            a, b = words[j1][1], words[j2 - 1][2]
                    elif tag == "delete":       # the model skipped the word: mute the gap
                        a = words[j1 - 1][2] if j1 > 0 else w0
                        b = words[j1][1] if j1 < len(words) else w0 + dur
                        if b - a < MIN_GAP_MUTE:
                            mid = (a + b) / 2
                            a, b = mid - MIN_GAP_MUTE / 2, mid + MIN_GAP_MUTE / 2
                    else:
                        continue
                    if b - a > MAX_SPAN:
                        unresolved.append(i)
                        continue
                    if add(a, b, 0.6, f"profanity(aligned): {toks[i]!r} in {text[:60]!r}"):
                        counts["aligned"] += 1
                        need -= 1
                        log(f"  {_fmt(a)} aligned {toks[i]!r} -> heard "
                            f"{' '.join(w[0] for w in words if w[1] < b and w[2] > a)!r}  [{text[:70]}]")
        else:
            unresolved = targets

        # 3. couldn't place it. First make sure the line itself is English —
        #    the padded window can pick up English from the lines around a
        #    subtitled foreign one — then mute the whole line.
        if unresolved and need > 0:
            own, own_info = model.transcribe(_audio(movie, s, e - s), language=None, **opts)
            own = list(own)
            if _is_foreign(own_info):
                foreign(own, own_info.language, s, s, text)
                continue
        if unresolved and need > 0 and add(s, e, 0.3, f"profanity(line): {text[:60]!r}"):
            counts["line"] += 1
            log(f"  {_fmt(s)} whole line muted (couldn't place {', '.join(toks[i] for i in unresolved)})"
                f"  [{text[:70]}]")

    log(f"Subtitle cross-check added {len(added)} mute(s): {counts['heard']} heard by "
        f"{model_name}, {counts['aligned']} placed by alignment, {counts['line']} whole line(s)"
        + (f" ({counts['foreign']} foreign-language line(s) checked word by word)" if counts["foreign"] else ""))
    return added
