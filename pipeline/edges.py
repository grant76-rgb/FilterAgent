"""Stretch word mutes to cover the whole audible word.

Whisper's word timestamps are approximate. On Daredevil they regularly
ended before the word did — "bitch" was muted 2045.06-2045.40, but the
"-tch" carried on to 2045.64 and would have stayed audible. Dialogue is
mixed into the centre channel, so its loudness there (200-4000 Hz) shows
where speech really is, with most music and effects left out.

Each word mute is extended to the edges of the speech burst it touches,
walking through short quiet gaps inside a word (a stop consonant like the
"t" in "bitch" is ~40 ms) and stopping at the first real pause. Mutes are
never shortened, and never extended by more than MAX_BEFORE / MAX_AFTER —
in fast speech the burst runs straight into the next word, so the caps bound
how much of a neighbouring word can be clipped.
"""

import json
import subprocess

import numpy as np

SR = 16000
HOP = 0.01             # 10 ms frames
MAX_BEFORE = 0.15
MAX_AFTER = 0.30
MAX_DIP = 0.06         # a quiet stretch shorter than this is still inside the word
DROP_DB = 18.0         # "quiet" = this far below the word's loudest frame...
FLOOR_MARGIN = 8.0     # ...and at least this far above the background level
WORD_REASONS = ("profanity(word)", "profanity(recheck)", "profanity(aligned)")

CREATE_NO_WINDOW = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0


def dialogue_filter(movie) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=channels,channel_layout", "-of", "json", str(movie)],
                       capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
    layout, ch = s.get("channel_layout", ""), int(s.get("channels") or 1)
    has_center = ch >= 3 and not layout.startswith(("stereo", "2.1", "quad", "4.0(", "2 "))
    pick = "pan=mono|c0=FC" if has_center else ("pan=mono|c0=0.5*c0+0.5*c1" if ch >= 2 else "anull")
    return pick + ",highpass=f=200,lowpass=f=4000"


def _envelope(movie, af, start, dur):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(movie), "-map", "0:a:0", "-af", af, "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    x = np.frombuffer(r.stdout, np.float32)
    n = int(HOP * SR)
    if len(x) < n:
        return np.array([])
    f = x[: len(x) // n * n].reshape(-1, n)
    return 20 * np.log10(np.sqrt((f.astype(np.float64) ** 2).mean(1)) + 1e-9)


def _walk(loud, i, step, limit_frames, max_dip_frames):
    """Furthest loud frame reachable from i in direction step, crossing quiet
    gaps shorter than max_dip_frames, within limit_frames."""
    best, gap, k = None, 0, i
    while 0 <= k < len(loud) and abs(k - i) <= limit_frames:
        if loud[k]:
            best, gap = k, 0
        else:
            gap += 1
            if gap >= max_dip_frames:
                break
        k += step
    return best


def refine(movie, ranges, log=print, only_word_mutes=True):
    """Return a copy of `ranges` with word mutes stretched to the audible word."""
    af = dialogue_filter(movie)
    out, ext = [], []
    for r in ranges:
        is_word = r.get("action") == 1 and (not only_word_mutes or
                                            str(r.get("reason", "")).startswith(WORD_REASONS))
        if not is_word:
            out.append(r)
            continue
        a, b = r["start"], r["end"]
        w0 = max(0.0, a - MAX_BEFORE - 0.5)
        db = _envelope(movie, af, w0, (b - w0) + MAX_AFTER + 0.5)
        if not len(db):
            out.append(r)
            continue
        t = w0 + np.arange(len(db)) * HOP
        floor = np.percentile(db, 20)
        body = db[(t >= a - 0.02) & (t <= b + MAX_AFTER)]
        peak = body.max() if len(body) else db.max()
        loud = db >= max(floor + FLOOR_MARGIN, peak - DROP_DB)
        ia = min(len(db) - 1, max(0, int(round((a - w0) / HOP))))
        ib = min(len(db) - 1, max(0, int(round((b - w0) / HOP)) - 1))
        dip = int(round(MAX_DIP / HOP))
        e_idx = _walk(loud, ib, +1, int(round(MAX_AFTER / HOP)), dip)
        s_idx = _walk(loud, ia, -1, int(round(MAX_BEFORE / HOP)), dip) if loud[ia] else None
        na = min(a, w0 + s_idx * HOP) if s_idx is not None else a
        nb = max(b, w0 + (e_idx + 1) * HOP) if e_idx is not None else b
        nr = dict(r, start=max(0.0, na), end=nb)
        out.append(nr)
        ext.append((a - nr["start"], nr["end"] - b))
    if ext:
        before = np.array([x[0] for x in ext]); after = np.array([x[1] for x in ext])
        log(f"Word edges: {len(ext)} mute(s) checked against the dialogue audio — "
            f"{int((after > 0.005).sum())} extended at the end (median +{np.median(after) * 1000:.0f} ms, "
            f"{int((after >= MAX_AFTER - 0.011).sum())} at the {MAX_AFTER * 1000:.0f} ms cap), "
            f"{int((before > 0.005).sum())} at the start")
    return out
