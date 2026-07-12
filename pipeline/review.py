"""Build a self-contained HTML contact sheet for human review.

Shows every proposed cut/mute with:
  - a thumbnail from the middle of the range (for cuts)
  - timestamp, duration, reason, confidence (low-confidence sorts first)
  - a checkbox to keep/discard
  - an 'Export EDL' button that regenerates the .edl text from checked rows
    so you can paste/save it over the auto-generated one

No server needed — everything is inlined, thumbnails as base64.
"""

import base64
import html
import json
from pathlib import Path


def _thumb_for(t: float, frames_dir: Path, fps: float) -> str:
    """Base64 jpeg of the sampled frame nearest to time t, or ''. """
    n = round(t * fps)
    for cand in (n, n - 1, n + 1, n - 2, n + 2):
        p = frames_dir / f"f_{cand:08d}.jpg"
        if p.exists():
            return base64.standard_b64encode(p.read_bytes()).decode()
    return ""


def build_contact_sheet(workdir: Path, movie: Path, cfg: dict) -> Path:
    ann = workdir / "edl_annotated.json"
    if not ann.exists():
        print("  No edl_annotated.json found — run the full pipeline first.")
        return None
    ranges = json.loads(ann.read_text())
    fps = cfg["sampling"]["fps"]
    frames_dir = workdir / "frames"
    skip_action = cfg["edl"].get("skip_action", 0)
    show_frames = cfg.get("review", {}).get("show_frames", True)
    edl_name = movie.with_suffix(".edl").name

    # low confidence first: those most need human eyes
    ranges.sort(key=lambda r: r["confidence"])

    rows = []
    for i, r in enumerate(ranges):
        mid = (r["start"] + r["end"]) / 2
        b64 = _thumb_for(mid, frames_dir, fps) if r["action"] == 0 else ""
        img = (f'<img src="data:image/jpeg;base64,{b64}">' if b64
               else '<div class="noimg">audio/mute</div>')
        kind = "CUT" if r["action"] == 0 else "MUTE"
        mins, secs = divmod(int(r["start"]), 60)
        reason = r["reason"][:160]
        kodi_action = skip_action if r["action"] == 0 else 1
        rows.append(f"""
        <tr data-start="{r['start']:.2f}" data-end="{r['end']:.2f}"
            data-action="{r['action']}" data-kodi-action="{kodi_action}"
            data-confidence="{r['confidence']:.4f}">
          <td><input type="checkbox" checked></td>
          <td class="frame-cell">{img}</td>
          <td class="k {kind.lower()}">{kind}</td>
          <td>{mins//60:d}:{mins%60:02d}:{secs:02d}</td>
          <td>{r['end']-r['start']:.1f}s</td>
          <td>{r['confidence']:.2f}</td>
          <td class="reason">{html.escape(reason)}</td>
        </tr>""")

    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Filter review — {html.escape(movie.stem)}</title>
<style>
 body{{font-family:system-ui;margin:20px;background:#111;color:#ddd}}
 table{{border-collapse:collapse;width:100%}}
 td{{padding:6px 10px;border-bottom:1px solid #333;vertical-align:middle}}
 img{{height:90px;border-radius:4px}}
 .noimg{{height:90px;width:160px;background:#222;display:flex;align-items:center;
        justify-content:center;color:#666;border-radius:4px;font-size:12px}}
 .k{{font-weight:bold}} .cut{{color:#f66}} .mute{{color:#fc6}}
 button{{padding:10px 18px;font-size:15px;margin:12px 0;cursor:pointer}}
 textarea{{width:100%;height:160px;background:#000;color:#0f0;font-family:monospace}}
 .controls{{display:flex;align-items:center;gap:8px;margin-bottom:8px}}
 select{{padding:4px 8px;font-size:14px}}
 body.hide-frames .frame-cell{{display:none}}
</style></head><body class="{'' if show_frames else 'hide-frames'}">
<h2>{html.escape(movie.stem)} — proposed filter ({len(ranges)} entries)</h2>
<div class="controls">
  <label for="sortSel">Sort by:</label>
  <select id="sortSel" onchange="sortRows(this.value)">
    <option value="confidence" selected>Certainty (least confident first)</option>
    <option value="time">Chronological</option>
  </select>
  <label><input type="checkbox" id="frameToggle" {'checked' if show_frames else ''}
         onchange="toggleFrames(this.checked)"> Show triggered frame</label>
  <span>Uncheck false positives, then export.</span>
</div>
<div class="controls">
  <button onclick="exportEdl()">Export EDL from checked rows</button>
  <button onclick="saveEdl()" title="First click asks you to pick the movie's folder; every click after that writes straight to it">Save / overwrite {html.escape(edl_name)}</button>
  <span id="saveStatus"></span>
</div>
<textarea id="out" placeholder="EDL text appears here — save as {html.escape(edl_name)}"></textarea>
<table><tbody id="rows">{''.join(rows)}</tbody></table>
<script>
const EDL_NAME = {json.dumps(edl_name)};

function toggleFrames(show){{
  document.body.classList.toggle('hide-frames', !show);
}}
function sortRows(key){{
  const tbody=document.getElementById('rows');
  const rows=[...tbody.querySelectorAll('tr')];
  rows.sort((a,b)=>{{
    const ka=key==='time'?parseFloat(a.dataset.start):parseFloat(a.dataset.confidence);
    const kb=key==='time'?parseFloat(b.dataset.start):parseFloat(b.dataset.confidence);
    return ka-kb;
  }});
  rows.forEach(r=>tbody.appendChild(r));
}}
function edlText(){{
  const lines=[...document.querySelectorAll('tr')]
    .filter(tr=>tr.dataset.start && tr.querySelector('input').checked)
    .map(tr=>`${{tr.dataset.start}}\\t${{tr.dataset.end}}\\t${{tr.dataset.kodiAction}}`);
  return lines.join('\\n') + (lines.length ? '\\n' : '');
}}
function exportEdl(){{
  document.getElementById('out').value = edlText();
}}

// Directory handle for the movie's folder, granted once per page session
// (via the browser's folder picker) so later saves write straight to the
// existing .edl with no further prompts — this is what makes "overwrite"
// actually overwrite instead of asking every time. Browsers won't let a
// page jump straight to an arbitrary local path without this one-time
// grant, even for a file:// page sitting right next to that folder.
let dirHandle = null;

async function saveEdl(){{
  const text = edlText();
  document.getElementById('out').value = text;
  const status = document.getElementById('saveStatus');

  if (window.showDirectoryPicker) {{
    try {{
      if (!dirHandle) {{
        status.textContent = 'Pick the folder that has ' + EDL_NAME + '...';
        dirHandle = await window.showDirectoryPicker({{ mode: 'readwrite' }});
      }}
      const fileHandle = await dirHandle.getFileHandle(EDL_NAME, {{ create: true }});
      const writable = await fileHandle.createWritable();
      await writable.write(text);
      await writable.close();
      status.textContent = 'Saved ' + EDL_NAME + ' in "' + dirHandle.name + '".';
      return;
    }} catch (e) {{
      if (e.name === 'AbortError') {{ status.textContent = 'Save cancelled.'; return; }}
      status.textContent = 'Could not write to that folder (' + e.message + ') — trying Save As instead.';
      dirHandle = null;
    }}
  }}

  if (window.showSaveFilePicker) {{
    try {{
      const handle = await window.showSaveFilePicker({{
        suggestedName: EDL_NAME,
        types: [{{description: 'EDL file', accept: {{'text/plain': ['.edl']}}}}],
      }});
      const writable = await handle.createWritable();
      await writable.write(text);
      await writable.close();
      status.textContent = 'Saved ' + handle.name;
    }} catch (e) {{
      if (e.name !== 'AbortError') status.textContent = 'Save failed: ' + e.message;
    }}
    return;
  }}

  const blob = new Blob([text], {{type: 'text/plain'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = EDL_NAME;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  status.textContent = 'Downloaded ' + EDL_NAME + ' — your browser can\\'t overwrite files directly, so move it over the original to replace it.';
}}
</script></body></html>"""

    sheet = workdir / "review.html"
    sheet.write_text(page)
    return sheet
