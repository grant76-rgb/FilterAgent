"""Post-run cleanup: move the review sheet next to the movie, then delete
the per-movie working directory (extracted frames/audio, cached scene and
analysis json) so only the mkv, edl, censored srt, and review sheet remain."""

import shutil
from pathlib import Path


def finalize(movie: Path, workdir: Path, sheet: Path | None,
             delete_workdir: bool = True, log=print) -> Path | None:
    """Moves the review sheet next to the movie. If delete_workdir is True,
    also removes the per-movie working directory (frames, cached scene and
    analysis data). Returns the review sheet's final path (or None)."""
    new_sheet = None
    if sheet and Path(sheet).exists():
        new_sheet = movie.parent / f"{movie.stem}.review.html"
        shutil.move(str(sheet), str(new_sheet))

    if delete_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            workdir.parent.rmdir()  # the shared .filteragent\ folder, if now empty
        except OSError:
            pass  # other movies still have cache subfolders in it — leave it
        log(f"Working files cleaned up ({workdir}).")
    else:
        log(f"Working files kept in {workdir}.")
    return new_sheet
