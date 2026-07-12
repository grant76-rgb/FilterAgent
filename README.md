# FilterAgent for Windows

Right-click any `.mkv` in Explorer → **Create filter EDL** → a Kodi-ready
`Movie.edl` appears next to the file, muting profanity from your own
wordlist and skipping scenes flagged by an automatic sexual-content filter.
It can also write a censored `Movie.en.srt` with banned words swapped for
asterisks, as an alternate subtitle track.

Built for personal-use playback filtering — the same kind of use protected
in the US by the Family Movie Act of 2005. It edits nothing: the original
`.mkv` is never touched, and Kodi just skips/mutes on the fly using the
`.edl` sidecar file.

- ✅ No re-encoding — original file stays untouched
- ✅ Runs locally — nothing leaves your machine
- ✅ Review sheet before anything is trusted — you approve every skip/mute
- ✅ Bring your own wordlist — ships with a starter list, not a demand

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [Use it](#use-it)
- [Kodi setup](#kodi-setup)
- [Files & folders](#files--folders)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Limitations](#limitations)

## How it works

1. **Extract** — ffmpeg pulls subtitles and 1 frame/sec out of the `.mkv`.
2. **Profanity → mute** — your wordlist is matched against subtitle cues.
   Entries are grouped into categories (`# category: NAME` in the
   wordlist) that can be toggled independently — see [Configure](#configure).
3. **Scene detection** — cut points are found so skips land cleanly instead
   of mid-shot.
4. **Sexual content → skip** — each sampled frame is scored by
   [NudeNet](https://github.com/notAI-tech/NudeNet), a local image
   classifier (nothing is uploaded anywhere).
5. **EDL written** — `Movie.edl` is saved next to `Movie.mkv`. Kodi reads
   it automatically; action `0` silently skips, action `1` mutes.
6. **Censored subtitles** (optional) — `Movie.en.srt` is written with
   muted words replaced by asterisks, for use as an alternate subtitle
   track. This is the only place words get censored — the review sheet
   (next) always shows the real match, since it's meant for you, not
   whoever's watching.
7. **Review sheet** — `Movie.review.html` shows a thumbnail of every
   proposed skip/mute, least-confident first, so you can uncheck false
   positives before trusting the result.
8. **Cleanup** (optional, on by default) — the per-movie working folder
   (`.filteragent\<name>\`: sampled frames, cached scene/analysis data)
   is deleted once the review sheet has been moved next to the movie,
   leaving only `.mkv`, `.edl`, `.en.srt`, and `.review.html`. The shared
   `.filteragent\` folder itself is removed too if that was the last
   movie's cache in it. Turn off "Delete working files after each run" in
   Settings > General to keep the cache instead — re-runs on that title
   will then reuse it and skip straight to whatever changed. If a run
   errors partway through, the working folder is always left in place
   for a retry, regardless of this setting.

A Windows toast notification fires when a run finishes (or fails), so you
don't have to babysit the progress window.

## Requirements

- Windows 10/11
- [Python 3.10+](https://www.python.org/downloads/)
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)
- ~500 MB free for Python packages, plus a few GB of scratch space per
  movie while it's being processed (see [Files & folders](#files--folders))

## Install

Takes about 20 minutes, once. After that it's right-click → wait → review.

### 1. Install Python

1. Download the latest Python 3 installer from
   [python.org/downloads](https://www.python.org/downloads/) (3.10 or
   newer).
2. Run it. **On the very first screen, check "Add python.exe to PATH"**
   before clicking Install Now — this is the step people miss, and
   nothing below works without it.
3. Verify in a **new** Command Prompt:

   ```
   python --version
   ```

   You should see something like `Python 3.12.x`. If you get
   `'python' is not recognized`, re-run the installer, choose **Modify**,
   enable "Add Python to environment variables", then open a brand-new
   Command Prompt (old windows don't pick up PATH changes).

### 2. Install ffmpeg

FilterAgent uses ffmpeg to pull subtitles and frames out of the file.

```
winget install ffmpeg
```

If `winget` isn't available, download the "release full" build from
[gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/), unzip it to
`C:\ffmpeg`, and add `C:\ffmpeg\bin` to your PATH (Start → "Edit the
system environment variables" → Environment Variables → Path → New).

Close Command Prompt and open a new one, then verify:

```
ffmpeg -version
```

### 3. Install FilterAgent

1. Clone or download this repo to a **permanent** location — not
   Downloads or Desktop, since the right-click menu points at this exact
   folder:

   ```
   git clone https://github.com/grant76-rgb/filteragent-win.git C:\Tools\FilterAgent
   ```

2. Install the Python packages:

   ```
   cd C:\Tools\FilterAgent
   pip install -r requirements.txt
   ```

   This pulls in the subtitle parser, scene detector, NudeNet image
   classifier, and toast-notification support — a few hundred MB, a few
   minutes.

3. Add the right-click menu entry:

   ```
   python install_context_menu.py
   ```

   You should see `Installed. Right-click any .mkv -> 'Create filter EDL'.`
   No admin rights needed — it only touches your user account.

> **Windows 11:** the new entries appear under **"Show more options"** in
> the right-click menu (or Shift+Right-click), since Win11 hides classic
> menu entries by default.

## Configure

Right-click any `.mkv` → Show more options → **FilterAgent settings**
(or run `python app.py` from the install folder).

**Wordlist tab**
- Check "Mute profanity".
- The wordlist below is grouped into categories with a `# category: NAME`
  header line. The default list ships two: `profanity` (always on) and
  `religious_identity` — neutral exclamations and identity terms (god,
  gay, jew, queer, ...) that aren't slurs but frequently false-positive
  on innocuous lines ("thank god", "she's gay"). It's **off by default**;
  check "Also mute religious & identity terms" to enable it.
- `*` is a wildcard: `damn*` matches *damned*, *damning*, etc. Wildcards
  only extend the word, so `dammit` still needs its own line.
- "Write censored subtitles" outputs `Movie.en.srt` with muted words
  replaced by asterisks — an alternate subtitle track for Kodi. On by
  default.
- Leave mute padding at the defaults (0.25s before / 0.35s after) until
  you've watched a movie; adjust later if mutes clip words.
- Leave "Word-exact mutes via Whisper" off for now — see
  [Optional upgrades](#optional-upgrades).

**Sexual content tab**
- Check "Skip scenes with sexual content".
- Start with **Moderate**. **Strict** also flags lingerie/suggestive
  categories, meaning more false positives to prune in review.
- The per-category sliders let you dial in a specific line for a specific
  movie — moving any slider switches the preset to Custom.
- "Extra padding before/after a flagged moment" (default 2s) pads every
  skip. At 1 fps, detection only samples once a second, so gradual/partial
  exposure can cross the confidence threshold a few seconds after it
  actually starts — if skips consistently start a little late, raise this
  (and/or raise "Frames sampled per second" in the General tab for finer
  temporal resolution).

**General tab**
- Frames per second: leave at **1.0**. Doubling it doubles both
  thoroughness and processing time — save that for problem titles.
- "Show skipped notification in Kodi" is your call: silent skips are
  seamless, the notification makes it obvious a filter fired.
- Leave "Open review sheet when finished" checked.
- Leave "Delete working files after each run" checked unless you're
  actively tuning settings on a specific title — unchecking it keeps the
  `.filteragent` cache around so re-runs skip re-extraction/re-detection
  of whatever didn't change (see [Files & folders](#files--folders)).

Click **Save**. Settings live in `%APPDATA%\FilterAgent\` and apply to
every movie from then on.

## Use it

1. Right-click a `.mkv` → **Create filter EDL**. For TV, where sexual
   content is rare, use **Create filter EDL (profanity only)** instead —
   it skips scene detection and frame sampling entirely (the slow part)
   and only runs the wordlist pass, typically finishing in under a
   minute regardless of episode length. It's a one-off override for that
   file; your saved Sexual content settings aren't changed.
2. A progress window walks through extract → profanity → scenes → sexual
   content → writing EDL → censoring subtitles → cleaning up
   (profanity-only runs breeze through the scenes/sexual-content stages
   since there's nothing to do). Frame classification dominates the
   runtime otherwise: expect roughly **20–40 minutes for a 2-hour film**
   on CPU. The very first sexual-content run also downloads a larger,
   more accurate detection model (~100 MB, one time only, cached in
   `%APPDATA%\FilterAgent\models\`) — if that download fails (blocked
   network, proxy, etc.) it automatically falls back to the smaller
   model bundled with the app rather than failing the run.
3. A Windows notification fires when the run finishes (or fails), so you
   don't have to watch the window.
4. When it finishes, `MovieName.review.html` opens in your browser — a
   thumbnail sheet of every proposed skip/mute, sorted least-confident
   first.
   - Uncheck anything that's a false positive (sunsets and skin-tone
     close-ups occasionally fool the classifier).
   - If you unchecked anything, click **Export EDL**, copy the text, and
     save it over the `.edl` file next to your movie (Notepad is fine —
     keep the exact filename `MovieName.edl`).
5. The `.edl`, `.en.srt` (if enabled), and `.review.html` now sit next to
   the `.mkv`. The temporary working folder used during processing is
   deleted automatically — that's the whole output.

## Kodi setup

Nothing to install or configure. Kodi automatically loads `MovieName.edl`
when it plays `MovieName.mkv` from the same folder. Mutes mute, skips
skip. If your library lives on a NAS/share, the `.edl` (and `.srt`) just
need to travel with the file.

The censored `Movie.en.srt` is an external subtitle track. If the file
already has embedded subtitles, pick "Movie.en.srt" from Kodi's subtitle
menu to see the censored version instead.

To sanity-check: play the movie and jump near a known flagged timestamp
from the review sheet.

## Files & folders

| Path | Contents |
|---|---|
| `%APPDATA%\FilterAgent\` | Your `config.yaml` + `wordlist.txt` |
| `<movie folder>\.filteragent\<name>\` | Working folder (sampled frames, cached scene/analysis data) — a few GB per film. Deleted automatically when a run finishes (along with the shared `.filteragent\` folder itself, if it's now empty), unless "Delete working files after each run" is unchecked in Settings > General (then it persists so re-runs are faster) or the run errored partway through. |
| `<movie folder>\<name>.edl` | The output Kodi reads |
| `<movie folder>\<name>.en.srt` | Censored subtitles (if enabled) |
| `<movie folder>\<name>.review.html` | The review sheet — kept permanently as a record of what was flagged |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `'python' is not recognized` | Python isn't on PATH. Re-run the installer → Modify → check "Add Python to environment variables". Open a **new** Command Prompt. |
| `ffmpeg not found on PATH` in the progress window | ffmpeg isn't installed, or you're using a Command Prompt opened before installing it. Reboot if in doubt. |
| Right-click entry missing | On Win11 it's under "Show more options". Still missing: re-run `python install_context_menu.py` from the install folder. |
| Right-click entry does nothing | The install folder moved or was renamed. Re-run `python install_context_menu.py` from its new location. |
| `No text subtitles found` warning | The MKV has image-based (PGS) subs or none. Drop a matching `MovieName.srt` next to the file and re-run, or enable Whisper alignment to transcribe the audio directly. |
| First NSFW run stalls at start | It's downloading the detection model (~100 MB) — give it a minute. |
| Log says "Model download failed" | Your network/proxy is blocking the download (e.g. a captive portal or firewall intercepting it). The run still completes using the smaller built-in model — lower accuracy, but not fatal. To use the better model anyway, manually download [`640m.onnx`](https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx) from a machine/network that can reach it, and place it at `%APPDATA%\FilterAgent\models\640m.onnx`. |
| Mutes clip the start/end of words | Raise mute padding in Settings (try 0.4 / 0.5), or enable Whisper word-exact mutes. |
| Skips feel jarring mid-action | Lower the scene threshold in `%APPDATA%\FilterAgent\config.yaml` (`scenes: threshold:` from 27 down to ~22) for finer scene boundaries and re-run. (If the previous run errored partway and left a `.filteragent` folder behind, delete it first so stale cached data isn't reused.) |
| It missed something | Raise sensitivity (Strict preset, or the relevant category slider), or raise frames-per-second to 2.0 for that title, and re-run. Always do the review pass — no automated filter is 100%. |
| Innocuous lines like "thank god" or "she's gay" get muted | The `religious_identity` wordlist category is enabled. Uncheck "Also mute religious & identity terms" in Settings > Wordlist. |

## Uninstall

```
python install_context_menu.py --remove
```

Then delete the install folder and `%APPDATA%\FilterAgent`.

## Optional upgrades

- **Word-exact mutes** — if cue-level muting silences too much dialogue:
  `pip install faster-whisper`, then enable "Word-exact mutes via
  Whisper" in Settings. Adds ~10–20 min per movie but mutes only the word
  itself.
- **Single .exe, no Python required on the target machine** — once you're
  happy with behavior:

  ```
  pip install pyinstaller
  pyinstaller --onedir --windowed --add-data "defaults;defaults" app.py
  ```

  Then point `install_context_menu.py`'s command at the built exe.

## Limitations

- The wordlist ships nearly empty on purpose — bring your own.
- NSFW detection is frame-sampled at 1fps by default; sub-second flashes
  can be missed. Raise `sampling.fps` for problem titles (slower).
- Sunsets, skin-tone close-ups, and similar scenes can occasionally
  trigger false positives — that's what the review sheet is for.
- **Always do the review pass before family viewing.** Do not trust the
  auto-generated EDL blind.
- This project intentionally does **not** modify or redistribute the
  underlying video/audio — it only generates a sidecar EDL (and
  optionally a censored subtitle track), consistent with personal-use
  filtering exemptions like the Family Movie Act. It is not legal advice;
  check the rules in your jurisdiction.

## License

[MIT](LICENSE)

## Support

If this saves you time, a [GitHub Sponsors](https://github.com/sponsors/grant76-rgb)
contribution is appreciated but never required — this project is and will
stay free and open source.
