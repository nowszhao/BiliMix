"""Content-addressed synthesis cache; shared by initial and resumed synthesis."""
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
    for model_root in (root, root/"checkpoints"):
        for pattern in ("*.pt", "*.pth", "*.safetensors", "*.ckpt", "*.bin"):
            files.update(model_root.rglob(pattern))
    records = []
    for path in sorted(files):
        if path.is_file():
            st = path.stat()
            records.append((str(path), _content(str(path), st.st_size, st.st_mtime_ns)))
    return canonical_hash(records)


def synthesis_key(text, language, reference, model_id, parameters, speaker="", reference_mode="speaker"):
    return canonical_hash({"schema": 3, "text": text, "language": language,
                           "reference_sha256": file_sha256(reference), "speaker": speaker,
                           "model": model_id, "parameters": parameters, "reference_mode": reference_mode,
                           "reference_policy": REFERENCE_POLICY_VERSION})
