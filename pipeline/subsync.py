"""Line up a downloaded or external .srt with the video.

Subtitles fetched online are often timed for a different release of the same
title (a streaming version with a different intro, say), so the whole file
runs seconds early or late — Daredevil S1 ranged from 0.1 s to 13 s off. The
disc's own subtitle track (usually image-based PGS, so its text is no use) is
timed exactly to this file, and the pattern of when lines appear and
disappear is distinctive enough to find the offset reliably: the right shift
typically overlaps 85-100 % of the .srt's on-screen time with the reference.

Only a constant shift is applied. If the two halves of the file disagree
(timing drift, or a scene missing from one release), nothing is changed.
"""

import json
import subprocess
from pathlib import Path

RES = 0.05            # seconds per bin
MAX_SHIFT = 60.0      # search range, seconds either way
MIN_SHIFT = 0.1       # smaller offsets aren't worth rewriting the file for
MIN_COVERAGE = 0.6    # share of the .srt's on-screen time that must line up after shifting
MAX_HALF_SPREAD = 0.5 # first/second half must agree this closely (s)

CREATE_NO_WINDOW = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0


def _ffprobe(args):
    r = subprocess.run(["ffprobe", "-v", "error", "-of", "json", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    return json.loads(r.stdout or "{}")


def reference_intervals(movie: Path):
    """(intervals, label) for the file's best embedded subtitle track, or ([], None)."""
    streams = [s for s in _ffprobe(["-show_streams", str(movie)]).get("streams", [])
               if s.get("codec_type") == "subtitle"]
    if not streams:
        return [], None

    def rank(s):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        return (not tags.get("language", "").startswith("en"),
                bool((s.get("disposition") or {}).get("forced")),
                "comment" in tags.get("title", "").lower())
    ref = sorted(streams, key=rank)[0]
    packets = _ffprobe(["-select_streams", str(ref["index"]), "-show_entries",
                        "packet=pts_time,duration_time,size", str(movie)]).get("packets", [])
    pk = []
    for p in packets:
        try:
            pk.append((float(p["pts_time"]), float(p.get("duration_time") or 0), int(p.get("size") or 0)))
        except (KeyError, ValueError):
            continue
    out = []
    if ref.get("codec_name") == "hdmv_pgs_subtitle":
        # a display set with an image starts a line; the next (tiny, empty) one clears it
        for (t, _d, size), nxt in zip(pk, pk[1:]):
            if size > 100 and nxt[0] > t:
                out.append((t, nxt[0]))
    else:
        out = [(t, t + d) for t, d, _s in pk if d > 0]
    label = f"track #{ref['index']} ({ref.get('codec_name')})"
    return out, label


def _best_shift(srt_iv, ref_iv):
    import numpy as np
    end = max(e for _, e in srt_iv + ref_iv) + MAX_SHIFT + 1
    n = int(end / RES)

    def raster(iv):
        a = np.zeros(n, np.float64)
        for s, e in iv:
            i, j = max(0, int(s / RES)), min(n, max(int(s / RES) + 1, int(e / RES)))
            a[i:j] = 1
        return a
    a, b = raster(ref_iv), raster(srt_iv)
    if not b.sum():
        return 0.0, 0.0
    m = 2 * n
    corr = np.fft.irfft(np.fft.rfft(a, m) * np.conj(np.fft.rfft(b, m)), m)   # corr[k] = sum a[i+k] b[i]
    k_max = int(MAX_SHIFT / RES)
    ks = np.arange(-k_max, k_max + 1)
    vals = corr[ks % m]
    k = int(ks[int(np.argmax(vals))])
    return k * RES, float(vals.max()) / float(b.sum())


def find_shift(srt_path: Path, movie: Path):
    """Seconds to ADD to every .srt time, plus a short explanation. Shift is
    None when there's no reference or the match isn't trustworthy."""
    import pysrt
    ref, label = reference_intervals(movie)
    if len(ref) < 20:
        return None, "no usable subtitle track in the video to sync against"
    subs = pysrt.open(str(srt_path), encoding="utf-8", error_handling=pysrt.ERROR_PASS)
    iv = [(c.start.ordinal / 1000, c.end.ordinal / 1000) for c in subs]
    if len(iv) < 20:
        return None, "too few subtitle lines to sync"
    shift, cover = _best_shift(iv, ref)
    if cover < MIN_COVERAGE:
        return None, f"no confident match against {label} (best {shift:+.2f}s covers {cover:.0%})"
    mid = iv[len(iv) // 2][0]
    halves = [_best_shift([x for x in iv if (x[0] < mid) == first], ref)[0] for first in (True, False)]
    if abs(halves[0] - halves[1]) > MAX_HALF_SPREAD:
        return None, (f"timing drifts ({halves[0]:+.2f}s in the first half, {halves[1]:+.2f}s in the "
                      f"second) — left unchanged")
    return shift, f"{shift:+.2f}s against {label}, {cover:.0%} of lines aligned"


def shift_srt(src: Path, dest: Path, shift: float):
    import pysrt
    subs = pysrt.open(str(src), encoding="utf-8", error_handling=pysrt.ERROR_PASS)
    out = pysrt.SubRipFile()
    for c in subs:
        s = c.start.ordinal / 1000 + shift
        e = c.end.ordinal / 1000 + shift
        if e <= 0:
            continue
        out.append(pysrt.SubRipItem(index=len(out) + 1, text=c.text,
                                    start=pysrt.SubRipTime(milliseconds=round(max(0.0, s) * 1000)),
                                    end=pysrt.SubRipTime(milliseconds=round(e * 1000))))
    out.save(str(dest), encoding="utf-8")


def sync_srt(srt_path: Path, movie: Path, log=print, dest: Path | None = None) -> float:
    """Sync srt_path to the video. Writes to dest (default: in place).
    Returns the shift applied (0.0 if none)."""
    try:
        shift, why = find_shift(srt_path, movie)
    except ImportError as e:
        log(f"Subtitle sync skipped ({e.name} not installed)")
        return 0.0
    if shift is None or abs(shift) < MIN_SHIFT:
        if shift is None:
            log(f"Subtitle sync: {why}")
        if dest is not None and dest != srt_path:
            dest.write_bytes(srt_path.read_bytes())
        return 0.0
    shift_srt(srt_path, dest or srt_path, shift)
    log(f"Subtitles were out of sync with the video — shifted {why}")
    return shift
