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


def paced_model_fingerprint(root, worker, *, metadata_only=False):
    """Hash the offline resources and policy used by the Mandarin worker.

    Direct TTS also has a completed-job cache above the worker queue. Its
    submission identity uses cheap file metadata so HTTP never reads weights.
    The worker's actual WAV key binds every resource by its contents.
    """
    root = Path(root)
    worker = Path(worker)
    files = [worker, worker.with_name("paced_mandarin.py"), Path(__file__).with_name("tts_narration.py")]
    files.extend(root / name for name in (
        "melo/config.json", "melo/checkpoint.pth", "converter/config.json",
        "converter/checkpoint.pth", "base_speakers/ses/zh.pth",
    ))
    for folder in (root / "frontend" / "hf" / "hub", root / "frontend" / "nltk"):
        files.extend(p for p in folder.rglob("*")
                     if p.is_file() and ".locks" not in p.parts and p.suffix != ".lock")
    records = []
    for path in files:
        try:
            stat = path.stat()
        except FileNotFoundError:
            if not metadata_only:
                raise FileNotFoundError(f"Missing paced Mandarin resource: {path}") from None
            identity = None  # Let the queued worker report the missing asset.
        else:
            identity = ((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) if metadata_only
                        else _content(str(path), stat.st_size, stat.st_mtime_ns))
        records.append((str(path), identity))
    return canonical_hash(records)


def synthesis_key(text, language, reference, model_id, parameters, speaker="", reference_mode="speaker"):
    return canonical_hash({"schema": 4, "text": text, "language": language,
                           "reference_sha256": file_sha256(reference), "speaker": speaker,
                           "model": model_id, "parameters": parameters, "reference_mode": reference_mode,
                           "reference_policy": REFERENCE_POLICY_VERSION})
