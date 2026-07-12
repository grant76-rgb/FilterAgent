"""Merge ranges and write the Kodi EDL next to the movie."""

import json
from pathlib import Path


def _merge(ranges, gap):
    if not ranges:
        return []
    ranges = sorted(ranges, key=lambda r: r["start"])
    out = [dict(ranges[0])]
    for r in ranges[1:]:
        cur = out[-1]
        if r["start"] <= cur["end"] + gap:
            cur["end"] = max(cur["end"], r["end"])
            cur["reason"] += " | " + r["reason"]
            cur["confidence"] = max(cur["confidence"], r["confidence"])
        else:
            out.append(dict(r))
    return out


def write(movie: Path, ranges, workdir: Path, cfg: dict, log=print) -> Path:
    gap = cfg["edl"]["merge_gap"]
    min_len = cfg["edl"]["min_skip_length"]
    skip_action = cfg["edl"].get("skip_action", 0)

    cuts = _merge([r for r in ranges if r["action"] == 0], gap)
    cuts = [c for c in cuts if c["end"] - c["start"] >= min_len]
    mutes = _merge([r for r in ranges if r["action"] == 1], gap)

    def inside_cut(m):
        return any(c["start"] <= m["start"] and m["end"] <= c["end"] for c in cuts)
    mutes = [m for m in mutes if not inside_cut(m)]

    # `action` stays 0 (cut) / 1 (mute) as a semantic marker for the review
    # page's thumbnail logic — the Kodi EDL action code is resolved here,
    # separately, so it doesn't clobber that marker.
    final = sorted(cuts + mutes, key=lambda r: r["start"])

    edl_path = movie.with_suffix(".edl")
    lines = [f"{r['start']:.2f}\t{r['end']:.2f}\t"
             f"{skip_action if r['action'] == 0 else 1}" for r in final]
    edl_path.write_text("\n".join(lines) + ("\n" if lines else ""))

    (workdir / "edl_annotated.json").write_text(json.dumps(final, indent=1))

    cut_total = sum(c["end"] - c["start"] for c in cuts)
    log(f"{len(cuts)} skips ({cut_total/60:.1f} min removed), "
        f"{len(mutes)} mutes")
    return edl_path
