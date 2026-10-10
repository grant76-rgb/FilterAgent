"""Second and third opinions for the sexual-content pass.

NudeNet on its own (a body-part detector) produced mostly false positives on
real films -- hands in a lap, a studio microphone, bare male chests labelled
FEMALE_BREAST_EXPOSED -- while missing nudity it can't parse: dark shots,
prone or crouched bodies, wide shots. Two more models fix that:

  ViT NSFW classifier -- one whole-frame "is this NSFW" score. Catches scenes
                         NudeNet misses, but has its own false-positive mode
                         (dark, tangled fight footage).
  CLIP ViT-L/14       -- zero-shot "naked vs. clothed" judgement against
                         paired text prompts. Too slow to run on every frame,
                         so it only checks frames the other two nominate, and
                         it has the final say.

The three models make different mistakes, which is the point: a frame is cut
only if NudeNet or the classifier nominates it AND CLIP confirms it.

Model files are downloaded once to %APPDATA%\\FilterAgent\\models\\ (pinned to
exact repository revisions so results can't drift). CLIP's text side is
precomputed into clip_prompts.npz, so the text model and tokenizer are never
needed at runtime.
"""

import io
import os
import shutil
import urllib.request
from pathlib import Path

import numpy as np

HF = "https://huggingface.co/{repo}/resolve/{rev}/{file}"
MODELS = {
    # Apache-2.0. int8 build of a ViT-B/16 @384 fine-tuned for sfw/nsfw.
    "vit": ("vit_nsfw_int8.onnx",
            HF.format(repo="AdamCodd/vit-base-nsfw-detector",
                      rev="8587de998f441aac03fdd57a85d2e4cb808c7d64",
                      file="onnx/model_quantized.onnx"),
            80_000_000),
    # OpenAI CLIP ViT-L/14 (MIT), ONNX int8 export of the image encoder.
    "clip": ("clip_l14_vision_int8.onnx",
             HF.format(repo="Xenova/clip-vit-large-patch14",
                       rev="c307790166907339eed5a9a53a249af534102536",
                       file="onnx/vision_model_quantized.onnx"),
             290_000_000),
}
PROMPTS = Path(__file__).parent / "clip_prompts.npz"

CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)


def _models_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    d = base / "FilterAgent" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download(url: str, dest: Path, min_bytes: int):
    """Downloads to a .part file and only renames it into place once its size
    looks like a real model file, so a cached HTML error page can't break
    every later run."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    size = tmp.stat().st_size
    if size < min_bytes:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded file too small ({size} bytes) — "
                           "likely blocked or redirected")
    tmp.replace(dest)


def _session(path: Path):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = os.cpu_count() or 4
    return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])


def read_frame(path: Path):
    """Returns (raw_bytes, RGB PIL image), or None if the frame can't be read
    — a missing or truncated frame is skipped instead of crashing the run.
    NudeNet is handed the bytes (it decodes them itself), which also avoids
    cv2.imread's failure on non-ASCII Windows paths."""
    from PIL import Image
    try:
        data = Path(path).read_bytes()
        img = Image.open(io.BytesIO(data))
        img.load()
        return data, img.convert("RGB")
    except Exception:
        return None


class Verifier:
    """Loaded only if both model files are available."""

    def __init__(self, vit_path: Path, clip_path: Path):
        self.vit = _session(vit_path)
        self.vit_in = self.vit.get_inputs()[0].name
        self.clip = _session(clip_path)
        p = np.load(PROMPTS)
        self.text = p["embeddings"].astype(np.float32)
        self.n_nude = int(p["n_nude"])

    # -- ViT: whole-frame NSFW probability, every sampled frame -------------
    def vit_scores(self, images) -> np.ndarray:
        """images: RGB PIL images. Batches of 16 match how the thresholds
        were calibrated (the int8 model's activations are batch-scaled)."""
        from PIL import Image
        out = []
        for i in range(0, len(images), 16):
            x = np.stack([
                ((np.asarray(im.resize((384, 384), Image.BILINEAR), np.float32)
                  / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)
                for im in images[i:i + 16]])
            logits = self.vit.run(None, {self.vit_in: x})[0]
            e = np.exp(logits - logits.max(axis=1, keepdims=True))
            out.append((e / e.sum(axis=1, keepdims=True))[:, 1])
        return np.concatenate(out) if out else np.zeros(0)

    # -- CLIP: naked-vs-clothed judgement, nominated frames only ------------
    def clip_nudity(self, image) -> float:
        """Max nudity probability over left/center/right square crops, so
        nudity at the edge of a wide frame isn't cropped away."""
        from PIL import Image
        w, h = image.size
        s = min(w, h)
        if w >= h:
            crops = [image.crop((x, 0, x + s, s)) for x in (0, (w - s) // 2, w - s)]
        else:
            y = (h - s) // 2
            crops = [image.crop((0, y, s, y + s))]
        best = 0.0
        for c in crops:
            x = np.asarray(c.resize((224, 224), Image.BICUBIC), np.float32) / 255.0
            x = ((x - CLIP_MEAN) / CLIP_STD).transpose(2, 0, 1)[None]
            emb = self.clip.run(None, {"pixel_values": x})[0][0]
            emb = emb / np.linalg.norm(emb)
            logits = 100.0 * (self.text @ emb)
            p = np.exp(logits - logits.max())
            p /= p.sum()
            best = max(best, float(p[:self.n_nude].sum()))
        return best


def load(log=print):
    """Returns a Verifier, downloading model files on first use, or None if
    they can't be fetched (the caller then falls back to NudeNet only)."""
    d = _models_dir()
    paths = {}
    for key, (name, url, min_bytes) in MODELS.items():
        dest = d / name
        if not dest.exists():
            mb = min_bytes // 1_000_000
            log(f"Downloading {key} verification model (~{mb} MB, one-time)...")
            try:
                _download(url, dest, min_bytes)
            except Exception as e:
                log(f"Model download failed ({e}).")
                return None
        paths[key] = dest
    try:
        return Verifier(paths["vit"], paths["clip"])
    except Exception as e:
        log(f"Failed to load verification models ({e}) — discarding them.")
        for p in paths.values():
            p.unlink(missing_ok=True)
        return None
