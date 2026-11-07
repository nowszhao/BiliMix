"""Download the offline resources for the duration-controlled Mandarin worker."""
import argparse
import os
from pathlib import Path


def prepare(root):
    # This command is the explicit network boundary. Workers run offline.
    os.environ["HF_HOME"] = str(root / "frontend" / "hf")
    os.environ["NLTK_DATA"] = str(root / "frontend" / "nltk")
    nltk_dir = root / "frontend" / "nltk"
    nltk_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    nltk_dir.chmod(0o700)
    from huggingface_hub import hf_hub_download, snapshot_download
    import nltk

    resources = [
        ("myshell-ai/MeloTTS-Chinese", "af5d207a364ea4208c6f589c89f57f88414bdd16",
         "config.json", "melo/config.json"),
        ("myshell-ai/MeloTTS-Chinese", "af5d207a364ea4208c6f589c89f57f88414bdd16",
         "checkpoint.pth", "melo/checkpoint.pth"),
        ("myshell-ai/OpenVoiceV2", "f36e7edfe1684461a8343844af60babc2efbb727",
         "converter/config.json", "converter/config.json"),
        ("myshell-ai/OpenVoiceV2", "f36e7edfe1684461a8343844af60babc2efbb727",
         "converter/checkpoint.pth", "converter/checkpoint.pth"),
        ("myshell-ai/OpenVoiceV2", "f36e7edfe1684461a8343844af60babc2efbb727",
         "base_speakers/ses/zh.pth", "base_speakers/ses/zh.pth"),
    ]
    import shutil
    for repo, revision, name, destination in resources:
        target = root / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        source = hf_hub_download(repo, name, revision=revision)
        shutil.copyfile(source, target)
    patterns = ["config.json", "tokenizer*", "vocab*", "special_tokens_map.json"]
    for repo in ("bert-base-multilingual-uncased", "bert-base-uncased",
                 "tohoku-nlp/bert-base-japanese-v3"):
        selected = patterns + (["model.safetensors"] if repo == "bert-base-multilingual-uncased" else [])
        snapshot_download(repo, allow_patterns=selected)
    for name in ("cmudict", "averaged_perceptron_tagger", "averaged_perceptron_tagger_eng"):
        if not nltk.download(name, download_dir=str(root / "frontend" / "nltk")):
            raise RuntimeError(f"Failed to prepare NLTK {name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    prepare(Path(args.output).expanduser().resolve())
