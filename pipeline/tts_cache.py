"""Content-addressed synthesis cache; shared by initial and resumed synthesis."""
import os
from functools import lru_cache
from pathlib import Path
from pipeline.dubbing_plan import canonical_hash, file_sha256
from pipeline.tts_references import REFERENCE_POLICY_VERSION


@lru_cache(maxsize=64)
def _content(path, size, mtime_ns):
    return file_sha256(path)


def model_fingerprint(root, worker, config_path):
    files = {Path(worker), Path(config_path)}
    root = Path(root)
    # Engine code and the worker's narration policy affect phrasing and seeds,
    # even when weights and YAML configuration have not changed.
    for code_root in (root/'confuciustts', root/'external'):
        files.update(code_root.rglob('*.py'))
    files.add(Path(__file__).with_name('tts_narration.py'))
    for model_root in (root, root/"checkpoints"):
        for pattern in ("*.pt", "*.pth", "*.safetensors", "*.ckpt", "*.bin"):
            files.update(model_root.rglob(pattern))
    records = []
    for path in sorted(files):
        if path.is_file():
            st = path.stat()
            records.append((str(path), _content(str(path), st.st_size, st.st_mtime_ns)))
    return canonical_hash(records)


def voxcpm_model_fingerprint(worker, model_ref, *, metadata_only=False):
    """Bind worker code, narration policy and the VoxCPM2 model identity.

    A Hugging Face repo id is hashed as a string; a local weight directory
    also contributes its weight files so a model swap invalidates the cache.
    """
    worker = Path(worker)
    files = [worker, Path(__file__).with_name("tts_narration.py")]
    root = Path(os.path.expanduser(str(model_ref)))
    if root.is_dir():
        for pattern in ("*.safetensors", "*.pt", "*.pth", "config.json"):
            files.extend(sorted(root.rglob(pattern)))
    records = []
    for path in files:
        if not path.is_file():
            continue
        st = path.stat()
        identity = ((st.st_size, st.st_mtime_ns) if metadata_only
                    else _content(str(path), st.st_size, st.st_mtime_ns))
        records.append((str(path), identity))
    records.append(("model_ref", str(model_ref)))
    return canonical_hash(records)


def synthesis_key(text, language, reference, model_id, parameters, speaker="", reference_mode="speaker"):
    return canonical_hash({"schema": 4, "text": text, "language": language,
                           "reference_sha256": file_sha256(reference), "speaker": speaker,
                           "model": model_id, "parameters": parameters, "reference_mode": reference_mode,
                           "reference_policy": REFERENCE_POLICY_VERSION})
