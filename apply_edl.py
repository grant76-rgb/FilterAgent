#!/usr/bin/env python3
"""
apply_edl.py -- bake a FilterAgent / Kodi .edl into the video file itself, for
players that ignore EDL sidecars (Plex, Jellyfin, smart-TV apps, phones).

  mute ranges (action 1)      The audio is silenced over each range. Video,
                              chapters and the other tracks are copied as-is,
                              so this is fast (a minute or two per episode)
                              and the picture is bit-for-bit untouched. Only
                              the audio is re-encoded, in the source's codec
                              and bitrate (lossless sources go to FLAC).

  skip ranges (action 0 / 3)  The range is cut out of the file. Cutting means
                              re-encoding the video (slow: tens of minutes per
                              film on CPU). Chapters and the censored .srt are
                              shifted to match; embedded subtitle tracks are
                              dropped because they would no longer line up.

The censored "<name>.en.srt" FilterAgent writes is embedded as an
"English (filtered)" subtitle track, and the original non-forced English
subtitle tracks (which still contain the words) are dropped, unless
--keep-subs.

Every output is checked before it's kept: duration, track count, and that
each muted range really is silent in the new file.

USAGE
  Separate filtered library (originals untouched):
    python apply_edl.py "C:\\TV\\Marvel's Daredevil" --out-dir "C:\\TV (Filtered)"
      Mirrors the folder layout under --out-dir, relative to --root
      (default: the input's parent folder, so the show folder is kept).
      Episodes whose EDL is empty are hard-linked so seasons stay complete.

  Replace in place (original kept as a backup):
    python apply_edl.py "C:\\TV\\Marvel's Daredevil" --in-place --backup-dir "C:\\FilterAgent originals"
      Each original, plus its .edl and .en.srt, moves into the backup folder
      under its full path. Keep the backup folder OUTSIDE every Plex library
      folder, or Plex will list the originals too.

  Several files/folders can be given at once. A file FilterAgent is still
  working on, or one that's in use (e.g. playing in Plex), is left alone.

  Options: --dry-run  --force  --keep-original-audio  --keep-subs
           --crf N / --preset P  (video quality, only used when cutting)

A folder is processed recursively; files without an .edl next to them are
skipped, as are files this script has already filtered.
"""

import argparse
import datetime
import errno
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000 if os.name == "nt" else 0
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts"}
MARK = "FILTERAGENT_EDL_APPLIED"
ZERO_LEN_MUTE = 0.2     # Whisper sometimes gives a word start == end; mute this much around it
SILENT_DB = -60.0       # a muted range must peak below this in the output
VERIFY_INSET = 0.015    # ignore codec edge smear (one E-AC-3 block is ~5 ms) at mute boundaries


def say(msg=""):
    print(msg, flush=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=CREATE_NO_WINDOW)


def probe(path: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", "-show_chapters", str(path)])
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"ffprobe failed on {path.name}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def tag(d: dict, key: str, default=""):
    for k, v in (d.get("tags") or {}).items():
        if k.lower() == key.lower():
            return v
    return default


# --- EDL ---------------------------------------------------------------------

def _parse_time(tok: str) -> float:
    if ":" in tok:                                   # hh:mm:ss.sss
        secs = 0.0
        for part in tok.split(":"):
            secs = secs * 60 + float(part)
        return secs
    return float(tok)


def read_edl(path: Path):
    mutes, cuts, ignored = [], [], 0
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line[1:2].isdigit():
                raise ValueError(f"{path.name} line {n}: frame-number EDL times "
                                 f"(#frame) aren't supported")
            continue
        parts = line.split()
        if len(parts) < 3:
            raise ValueError(f"{path.name} line {n}: can't parse {line!r}")
        start, end, action = _parse_time(parts[0]), _parse_time(parts[1]), int(float(parts[2]))
        if action == 1:
            mutes.append((start, end))
        elif action in (0, 3):
            cuts.append((start, end))
        else:
            ignored += 1                             # 2 = scene marker
    return mutes, cuts, ignored


def normalize(ranges, duration):
    """Clamp to the file, drop empty ranges, sort, merge overlaps."""
    out = sorted((max(0.0, s), min(duration, e)) for s, e in ranges)
    merged = []
    for s, e in out:
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(r) for r in merged]


def snap_to_frames(cuts, vstart, fps):
    """Move cut edges onto video frame times so video and audio lose exactly
    the same amount of time (otherwise each cut could drift A/V by a frame)."""
    out = []
    for s, e in cuts:
        s2 = vstart + math.ceil((s - vstart) * fps - 1e-6) / fps
        e2 = vstart + math.ceil((e - vstart) * fps - 1e-6) / fps
        if e2 > s2:
            out.append((s2, e2))
    return out


def shift(t, cuts):
    """Map an original timestamp onto the cut timeline. A time inside a cut
    lands where that cut now begins."""
    removed = 0.0
    for s, e in cuts:
        if t >= e:
            removed += e - s
        elif t > s:
            return s - removed
        else:
            break
    return t - removed


def inside_cut(t, cuts):
    return any(s <= t < e for s, e in cuts)


def between_expr(ranges, offset=0.0):
    return "+".join(f"between(t,{s + offset:.4f},{e + offset:.4f})" for s, e in ranges)


# --- encoding settings -------------------------------------------------------

def audio_codec_args(s: dict) -> list:
    codec = s.get("codec_name", "")
    ch = int(s.get("channels") or 2)
    profile = s.get("profile") or ""
    raw = s.get("bit_rate") or tag(s, "BPS")
    br = int(raw) if str(raw).isdigit() else 0
    lossless = (codec in ("truehd", "mlp", "flac", "alac") or codec.startswith("pcm_")
                or (codec == "dts" and ("MA" in profile or "X" in profile)))
    if lossless or (codec in ("ac3", "eac3", "dts") and ch > 6):
        return ["flac"]
    if codec == "eac3":
        return ["eac3", "-b:{i}", str(br or 640000)]
    if codec in ("ac3", "dts"):
        return ["ac3", "-b:{i}", str(min(br or 640000, 640000))]
    if codec == "aac":
        return ["aac", "-b:{i}", str(br or 64000 * ch)]
    if codec == "opus":
        return ["libopus", "-b:{i}", str(br or 48000 * ch)]
    if codec == "vorbis":
        return ["libvorbis", "-b:{i}", str(br or 64000 * ch)]
    if codec == "mp3":
        return ["libmp3lame", "-b:{i}", str(br or 192000)]
    return ["flac"]


def video_codec_args(v: dict, crf, preset) -> list:
    pix = v.get("pix_fmt", "")
    ten_bit = any(b in pix for b in ("10", "12"))
    if ten_bit:   # keep 10-bit (HDR10 static metadata is passed through by ffmpeg 7+)
        args = ["libx265", "-crf:{i}", str(crf if crf is not None else 20),
                "-preset:{i}", preset, "-pix_fmt:{i}", "yuv420p10le"]
    else:         # H.264 plays directly on every Plex client
        args = ["libx264", "-crf:{i}", str(crf if crf is not None else 18),
                "-preset:{i}", preset, "-pix_fmt:{i}", "yuv420p"]
    for key, opt in (("color_primaries", "color_primaries"), ("color_transfer", "color_trc"),
                     ("color_space", "colorspace"), ("color_range", "color_range")):
        if v.get(key) and v[key] != "unknown":
            args += [f"-{opt}:{{i}}", v[key]]
    return args


def _ffmeta_escape(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def write_shifted_chapters(chapters, cuts, path: Path) -> bool:
    lines = [";FFMETADATA1"]
    kept = 0
    for ch in chapters:
        s = shift(float(ch["start_time"]), cuts)
        e = shift(float(ch["end_time"]), cuts)
        if e - s < 1.0:
            continue                                 # chapter was (almost) entirely cut
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(s * 1000)}",
                  f"END={round(e * 1000)}"]
        title = tag(ch, "title")
        if title:
            lines.append(f"title={_ffmeta_escape(title)}")
        kept += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return kept > 0


def write_shifted_srt(src: Path, cuts, dest: Path):
    import pysrt
    subs = pysrt.open(str(src), encoding="utf-8", error_handling=pysrt.ERROR_PASS)
    out = pysrt.SubRipFile()
    for cue in subs:
        s = shift(cue.start.ordinal / 1000, cuts)
        e = shift(cue.end.ordinal / 1000, cuts)
        if e - s < 0.05:
            continue                                 # cue was inside a cut
        out.append(pysrt.SubRipItem(
            index=len(out) + 1, text=cue.text,
            start=pysrt.SubRipTime(milliseconds=round(s * 1000)),
            end=pysrt.SubRipTime(milliseconds=round(e * 1000))))
    out.save(str(dest), encoding="utf-8")


# --- core --------------------------------------------------------------------

def build_command(movie, info, mutes, cuts, srt, args, tmpdir: Path, dest: Path, notes: list):
    streams = info["streams"]
    videos = [s for s in streams if s["codec_type"] == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    subs = [s for s in streams if s["codec_type"] == "subtitle"]
    attachments = [s for s in streams if s["codec_type"] == "attachment"]
    if not videos or not audios:
        raise RuntimeError("needs at least one video and one audio track")
    v = videos[0]
    if len(videos) > 1:
        notes.append(f"only the first of {len(videos)} video tracks is kept")

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1", "-i", str(movie)]
    n_inputs = 1

    srt_input = None
    if srt:
        if cuts:
            shifted = tmpdir / "filtered.srt"
            write_shifted_srt(srt, cuts, shifted)
            srt = shifted
        cmd += ["-i", str(srt)]
        srt_input = n_inputs
        n_inputs += 1

    chapters_input = None
    if cuts and info.get("chapters"):
        chap_file = tmpdir / "chapters.txt"
        if write_shifted_chapters(info["chapters"], cuts, chap_file):
            cmd += ["-f", "ffmetadata", "-i", str(chap_file)]
            chapters_input = n_inputs
            n_inputs += 1

    # Filter graph: mute (on the original timeline) then cut. asetnsamples
    # splits audio into 64-sample (~1.3 ms) frames so mute edges land on the
    # exact EDL time instead of the codec's ~32 ms frame boundaries.
    # When cutting, timestamps are rebuilt from frame/sample counts; each
    # stream keeps its original start offset or A/V sync shifts by the
    # difference (e.g. video starting 58 ms after the audio).
    fmt_start = float(info["format"].get("start_time") or 0)

    def start_of(s):
        return max(0.0, float(s.get("start_time") or 0) - fmt_start)

    graph = []
    for k, a in enumerate(audios):
        chain = [f"[0:{a['index']}]asetnsamples=n=64:p=0"]
        if mutes:
            chain.append(f"volume=volume=0:enable='{between_expr(mutes)}'")
        if cuts:
            chain.append(f"aselect='not({between_expr(cuts)})',"
                         f"asetpts=N/SR/TB+{start_of(a):.6f}/TB")
        graph.append(",".join(chain) + f"[a{k}]")
    if cuts:
        # cut edges are on frame times; test half a frame early so float
        # rounding can't decide whether a boundary frame stays
        half = 0.5 / args._fps
        graph.append(f"[0:{v['index']}]select='not({between_expr(cuts, -half)})',"
                     f"setpts=N/FRAME_RATE/TB+{start_of(v):.6f}/TB[v0]")
    graph_file = tmpdir / "graph.txt"
    graph_file.write_text(";\n".join(graph), encoding="utf-8")
    cmd += ["-/filter_complex", str(graph_file)]

    out = []          # one entry per output stream: (map spec, [per-stream options])
    if cuts:
        # enc_time_base=filter keeps millisecond timestamps; the default
        # (one frame) would round the video's start offset to a whole frame
        out.append(("[v0]", ["-c:{i}"] + video_codec_args(v, args.crf, args.preset)
                    + ["-enc_time_base:{i}", "filter",
                       "-metadata:s:{i}", f"language={tag(v, 'language', 'und')}"]))
    else:
        out.append((f"0:{v['index']}", ["-c:{i}", "copy"]))

    for k, a in enumerate(audios):
        lang = tag(a, "language", "und")
        title = tag(a, "title")
        default = "default" if (a.get("disposition") or {}).get("default") or k == 0 else "0"
        out.append((f"[a{k}]", ["-c:{i}"] + audio_codec_args(a)
                    + ["-metadata:s:{i}", f"language={lang}",
                       "-metadata:s:{i}", f"title={title + ' (filtered)' if title else 'Filtered'}",
                       "-disposition:{i}", default]))
        if args.keep_original_audio:
            out.append((f"0:{a['index']}", ["-c:{i}", "copy",
                        "-metadata:s:{i}", f"title={title + ' ' if title else ''}(original, unfiltered)",
                        "-disposition:{i}", "0"]))

    if srt_input is not None:
        out.append((f"{srt_input}:0", ["-c:{i}", "srt", "-metadata:s:{i}", "language=eng",
                                       "-metadata:s:{i}", "title=English (filtered)",
                                       "-disposition:{i}", "0"]))
    dropped = []
    for s in subs:
        lang = tag(s, "language", "und")
        label = f"#{s['index']} {lang} {s.get('codec_name', '')} {tag(s, 'title')}".strip()
        if cuts:
            dropped.append(label)
            continue
        forced = (s.get("disposition") or {}).get("forced")
        if srt_input is not None and not args.keep_subs and lang in ("eng", "en") and not forced:
            dropped.append(label)
            continue
        out.append((f"0:{s['index']}", ["-c:{i}", "copy"]))
    if dropped:
        why = ("they'd be out of sync after cuts" if cuts
               else "uncensored; replaced by the filtered .srt")
        notes.append(f"dropped subtitle track(s) {', '.join(dropped)} ({why})")
    if not srt and subs:
        notes.append("no censored .en.srt found next to the file; original subtitles kept")

    for t in attachments:
        out.append((f"0:{t['index']}", ["-c:{i}", "copy"]))

    for i, (spec, opts) in enumerate(out):
        cmd += ["-map", spec] + [o.replace("{i}", str(i)) for o in opts]

    stamp = (f"{datetime.date.today().isoformat()} mutes={len(mutes)} "
             f"cuts={len(cuts)} from {movie.with_suffix('.edl').name}")
    cmd += ["-map_metadata", "0",
            "-map_chapters", str(chapters_input if chapters_input is not None else (-1 if cuts else 0)),
            "-metadata", f"{MARK}={stamp}",
            "-max_muxing_queue_size", "4096", "-f", "matroska", str(dest)]
    return cmd


def encode(cmd, duration, log_path: Path):
    with open(log_path, "w", encoding="utf-8", errors="replace") as errf:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
        next_pct = 10
        for line in p.stdout:
            if line.startswith("out_time_us=") and duration:
                try:
                    pct = int(line.split("=", 1)[1]) / 1e6 / duration * 100
                except ValueError:
                    continue
                if pct >= next_pct:
                    say(f"    ...{min(pct, 100):.0f}%")
                    next_pct = (int(pct) // 10 + 1) * 10
        code = p.wait()
    if code != 0:
        err = log_path.read_text(encoding="utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed (exit {code}): {err[-800:]}")


def verify(path: Path, info_in: dict, mutes, cuts, n_audio_min):
    o = probe(path)
    dur_in = float(info_in["format"]["duration"])
    expected = dur_in - sum(e - s for s, e in cuts)
    dur_out = float(o["format"]["duration"])
    if abs(dur_out - expected) > 1.0:
        raise RuntimeError(f"output is {dur_out:.1f}s long, expected {expected:.1f}s")
    if sum(s["codec_type"] == "audio" for s in o["streams"]) < n_audio_min:
        raise RuntimeError("output is missing audio tracks")

    checked, loud, unmeasured = 0, [], 0
    for s, e in mutes:
        a, b = s + VERIFY_INSET, e - VERIFY_INSET
        if b - a < 0.02 or inside_cut(a, cuts) or inside_cut(b, cuts):
            unmeasured += 1                          # too short, or cut out anyway
            continue
        na, nb = shift(a, cuts), shift(b, cuts)
        r = run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{na:.3f}", "-t", f"{nb - na:.3f}",
                 "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"])
        m = re.search(r"max_volume:\s*(-?inf|-?[\d.]+) dB", r.stderr)
        peak = float(m.group(1)) if m else float("-inf")   # no samples decoded -> treat as silent
        checked += 1
        if peak > SILENT_DB:
            loud.append(f"{s:.2f}-{e:.2f} peaks at {peak:.1f} dB")
    if loud:
        raise RuntimeError("muted ranges aren't silent: " + "; ".join(loud))
    return dur_out, checked, unmeasured


def backup_path(movie: Path, backup_dir: Path) -> Path:
    full = movie.resolve()
    return backup_dir / Path(*full.parts[1:])        # drop the drive: C:\TV\x -> backup\TV\x


def move_no_overwrite(src: Path, dst: Path):
    """Never overwrite, and never leave a half-move behind. A plain rename
    (same drive) fails cleanly if the file is open, e.g. being streamed;
    shutil.move would instead copy the whole file and then fail the delete."""
    if dst.exists():
        raise RuntimeError(f"won't overwrite existing {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if getattr(e, "winerror", None) != 17 and e.errno != errno.EXDEV:   # 17 = different drive
            raise
        shutil.copy2(src, dst)
        try:
            os.unlink(src)
        except OSError:
            dst.unlink(missing_ok=True)
            raise


def busy_movies() -> set:
    """Files a FilterAgent worker is still processing. Its .edl is written
    before the censored .srt and review sheet, so an .edl alone doesn't mean
    the run is finished."""
    if os.name != "nt":
        return set()
    r = run(["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'gui\\.worker' } "
             "| ForEach-Object { $_.CommandLine }"])
    busy = set()
    for line in r.stdout.splitlines():
        m = re.search(r'gui\.worker\s+"([^"]+)"', line) or re.search(r"gui\.worker\s+(\S+)", line)
        if m:
            busy.add(os.path.normcase(os.path.abspath(m.group(1))))
    return busy


def process(movie: Path, root: Path, args) -> str:
    edl = movie.with_suffix(".edl")
    name = movie.name
    if not edl.exists():
        say(f"SKIP {name}: no .edl next to it (not run through FilterAgent yet, or already filtered)")
        return "skipped"
    if os.path.normcase(os.path.abspath(movie)) in busy_movies():
        say(f"SKIP {name}: FilterAgent is still working on it")
        return "skipped"
    info = probe(movie)
    if tag(info["format"], MARK) and (args.in_place or not args.force):
        hint = (" -- to redo it, restore the original from the backup folder first"
                if args.in_place else "")
        say(f"SKIP {name}: already filtered ({tag(info['format'], MARK)}){hint}")
        return "skipped"

    duration = float(info["format"]["duration"])
    mutes, cuts, ignored = read_edl(edl)
    zero = sum(1 for s, e in mutes if e <= s)
    mutes = [(s - ZERO_LEN_MUTE / 2, s + ZERO_LEN_MUTE / 2) if e <= s else (s, e)
             for s, e in mutes]
    mutes = normalize(mutes, duration)
    cuts = normalize(cuts, duration)

    notes = []
    if zero:
        notes.append(f"{zero} zero-length mute(s) widened to {ZERO_LEN_MUTE}s")
    if ignored:
        notes.append(f"{ignored} EDL line(s) with other actions ignored")
    if cuts:
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        rate = v.get("r_frame_rate", "0/1")
        if Fraction(rate) == 0:
            rate = v.get("avg_frame_rate", "24000/1001")
        args._fps = float(Fraction(rate))
        vstart = float(v.get("start_time") or 0) - float(info["format"].get("start_time") or 0)
        cuts = snap_to_frames(cuts, vstart, args._fps)

    if args.in_place:
        final = movie.with_suffix(".mkv")
        tmp = movie.with_name(movie.name + ".filtering")      # not a video extension: Plex ignores it
    else:
        final = Path(args.out_dir) / movie.relative_to(root).with_suffix(".mkv")
        tmp = final.with_name(final.name + ".partial")
        if final.exists() and not args.force:
            say(f"SKIP {name}: {final} already exists")
            return "skipped"

    if not mutes and not cuts:
        if args.in_place:
            say(f"SKIP {name}: EDL has nothing to apply")
            return "skipped"
        say(f"LINK {name}: EDL has nothing to apply -> {final}")
        if not args.dry_run:
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(movie, final)
            except OSError:
                shutil.copy2(movie, final)
        return "ok"

    srt = movie.with_name(movie.stem + ".en.srt")
    srt = srt if srt.exists() and not args.no_srt else None
    cut_total = sum(e - s for s, e in cuts)
    say(f"{'PLAN' if args.dry_run else 'FILTER'} {name}")
    say(f"    {len(mutes)} mute(s)" + (f", {len(cuts)} cut(s) removing {cut_total / 60:.1f} min"
                                       " (video will be re-encoded)" if cuts else
                                       " (video copied untouched)"))

    if srt and not args.dry_run:
        # Downloaded subtitles are often timed for another release; line the
        # censored .srt up with the disc's own subtitle track first (fixes the
        # sidecar too, so Kodi gets the corrected timing as well).
        try:
            from pipeline import subsync
            shift, why = subsync.find_shift(srt, movie)
            if shift is not None and abs(shift) >= subsync.MIN_SHIFT:
                subsync.shift_srt(srt, srt, shift)
                notes.append(f"subtitles were out of sync with the video; shifted {why}")
            elif shift is None:
                notes.append(f"subtitle sync: {why}")
        except ImportError as e:
            notes.append(f"subtitle sync skipped ({e.name} not installed)")

    tmpdir = Path(tempfile.mkdtemp(prefix="apply_edl_"))
    try:
        cmd = build_command(movie, info, mutes, cuts, srt, args, tmpdir, tmp, notes)
        for n in notes:
            say(f"    note: {n}")
        say(f"    -> {final}")
        if args.in_place:
            bak = backup_path(movie, Path(args.backup_dir))
            sides = [p for p in (edl, srt) if p and p.exists()]
            for p in [bak] + [bak.parent / s.name for s in sides]:
                if p.exists():
                    raise RuntimeError(f"a backup already exists, won't overwrite it: {p}")
        if args.dry_run:
            return "ok"

        final.parent.mkdir(parents=True, exist_ok=True)
        encode(cmd, duration - cut_total, tmpdir / "ffmpeg.log")
        n_audio = sum(s["codec_type"] == "audio" for s in info["streams"])
        dur_out, checked, short = verify(tmp, info, mutes, cuts, n_audio)
        say(f"    verified: {dur_out / 60:.1f} min, {checked} mute(s) confirmed silent"
            + (f" ({short} too short to measure or inside a cut)" if short else ""))

        if args.in_place:
            try:
                move_no_overwrite(movie, bak)
            except OSError as e:
                raise RuntimeError(f"couldn't move the original aside (in use? playing?): {e}")
            try:
                tmp.replace(final)
            except OSError:
                move_no_overwrite(bak, movie)               # put the original back
                raise
            for side in sides:
                move_no_overwrite(side, bak.parent / side.name)
            say(f"    original + .edl/.srt moved to {bak.parent}")
        else:
            tmp.replace(final)
        say(f"    done: {final.name} ({final.stat().st_size / 2**30:.2f} GB)")
        return "ok"
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Bake a .edl (mutes/skips) into the video file.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("USAGE", 1)[1])
    ap.add_argument("paths", nargs="+", metavar="path",
                    help="video file(s) and/or folder(s); folders are processed recursively")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out-dir", help="write filtered copies under this folder")
    mode.add_argument("--in-place", action="store_true",
                      help="replace each file; the original goes to --backup-dir")
    ap.add_argument("--backup-dir", help="where originals go with --in-place "
                                         "(must be outside your Plex library folders)")
    ap.add_argument("--root", help="with --out-dir: mirror paths relative to this folder "
                                   "(default: the input's parent)")
    ap.add_argument("--keep-original-audio", action="store_true",
                    help="also keep the unfiltered audio as a second, non-default track "
                         "(mute-only files)")
    ap.add_argument("--keep-subs", action="store_true",
                    help="keep the original English subtitle tracks too")
    ap.add_argument("--no-srt", action="store_true", help="don't embed the censored .en.srt")
    ap.add_argument("--crf", type=int, help="video quality when cutting (default 18 x264 / 20 x265)")
    ap.add_argument("--preset", default="medium", help="x264/x265 preset when cutting")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="redo files already filtered / overwrite existing outputs")
    args = ap.parse_args()
    args._fps = 24000 / 1001

    if args.in_place and not args.backup_dir:
        ap.error("--in-place needs --backup-dir")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        ap.error("ffmpeg/ffprobe not found on PATH")

    jobs = []                                        # (movie, root)
    for target in map(Path, args.paths):
        if target.is_dir():
            found = sorted(p for p in target.rglob("*") if p.suffix.lower() in VIDEO_EXTS
                           and ".filteragent" not in p.parts)
        elif target.is_file():
            found = [target]
        else:
            ap.error(f"not found: {target}")
        root = Path(args.root) if args.root else target.parent
        if args.out_dir and Path(args.out_dir).resolve() == root.resolve():
            ap.error("--out-dir can't be the same folder as the source")
        jobs += [(m, root) for m in found]

    results = {"ok": 0, "skipped": 0, "failed": 0}
    for m, root in jobs:
        if args.keep_original_audio:
            try:
                if read_edl(m.with_suffix(".edl"))[1]:
                    say(f"SKIP {m.name}: --keep-original-audio can't be used on a file with cuts")
                    results["skipped"] += 1
                    continue
            except (OSError, ValueError):
                pass
        try:
            results[process(m, root, args)] += 1
        except Exception as e:
            say(f"FAIL {m.name}: {e}")
            results["failed"] += 1
    say(f"\n{results['ok']} done, {results['skipped']} skipped, {results['failed']} failed")
    sys.exit(1 if results["failed"] else 0)


if __name__ == "__main__":
    main()
