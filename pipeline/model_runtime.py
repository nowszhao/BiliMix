"""Portable model-cache discovery shared by worker and orchestration processes."""
from __future__ import annotations
import os
from pathlib import Path

DEFAULT_DIARIZATION_MODEL = 'pyannote/speaker-diarization-community-1'


def hub_cache_dir(environ=None):
    env = os.environ if environ is None else environ
    explicit = env.get('HF_HUB_CACHE') or env.get('HUGGINGFACE_HUB_CACHE')
    if explicit:
        return Path(explicit).expanduser()
    if env.get('HF_HOME'):
        return Path(env['HF_HOME']).expanduser()/'hub'
    base = Path(env['XDG_CACHE_HOME']).expanduser() if env.get('XDG_CACHE_HOME') else Path.home()/'.cache'
    return base/'huggingface'/'hub'


def model_snapshots(model=None, environ=None):
    env = os.environ if environ is None else environ
    model = model or env.get('BILIMIX_DIARIZATION_MODEL',DEFAULT_DIARIZATION_MODEL)
    local = Path(model).expanduser()
    if local.is_dir():
        return [local]
    directory = hub_cache_dir(env)/('models--'+model.replace('/','--'))/'snapshots'
    return sorted((p for p in directory.iterdir() if p.is_dir()),key=lambda p:p.name) if directory.is_dir() else []


def configure_diarization_environment(environ=None):
    env = os.environ if environ is None else environ
    model = env.get('BILIMIX_DIARIZATION_MODEL',DEFAULT_DIARIZATION_MODEL)
    # The community pipeline bundles these files. Do not assume an arbitrary
    # custom pipeline's external dependencies are already cached.
    required = ('config.yaml','embedding/pytorch_model.bin','segmentation/pytorch_model.bin','plda/plda.npz')
    if model == DEFAULT_DIARIZATION_MODEL and any(all((p/name).is_file() for name in required) for p in model_snapshots(model,env)):
        env.setdefault('HF_HUB_OFFLINE','1')
    return model


def whisperx_command(configured='whisperx', python=None):
    """Use the selected ASR runtime instead of an unrelated PATH launcher."""
    import sys
    if isinstance(configured,(list,tuple)):
        return list(configured)
    value = os.path.expanduser(str(configured))
    if Path(value).name.lower() not in ('whisperx','whisperx.exe') or Path(value).parent != Path('.'):
        return [value]
    python = python or sys.executable
    executable = Path(python).parent/('whisperx.exe' if os.name == 'nt' else 'whisperx')
    return [str(executable)] if executable.is_file() else [str(python),'-m','whisperx']
