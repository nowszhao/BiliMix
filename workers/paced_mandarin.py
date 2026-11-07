"""Generate a timed Mandarin guide with Melo, then change its timbre with OpenVoice V2.

Only the model worker instantiates this class. Timing is constrained at the
duration predictor, before Melo expands phonemes to mel frames and decodes PCM.
"""
import json
import os
from pathlib import Path
import time

from pipeline.dubbing_plan import file_sha256


PHONE_SECONDS = 0.32
BLANK_SECONDS = 0.08
PUNCTUATION_SECONDS = 0.65
PUNCTUATION = frozenset("!?…,.\u0027-¿¡") | {"SP"}


def _bounded_guide(model, inputs, symbols, speaker, frame_seconds):
    """Use the stock decoder and clamp its deterministic duration prediction."""
    import torch

    bert, ja_bert, phones, tones, lang_ids = inputs
    device = phones.device
    phone_symbols = [symbols[i] for i in phones.tolist()]
    budget = [
        BLANK_SECONDS if s == "_" else PUNCTUATION_SECONDS if s in PUNCTUATION else PHONE_SECONDS
        for s in phone_symbols
    ]
    ceilings = torch.tensor([max(1, int(s / frame_seconds)) for s in budget], device=device)
    spoken = torch.tensor([s != "_" and s not in PUNCTUATION for s in phone_symbols], device=device)
    if not spoken.any():
        raise ValueError("配音文本缺少可朗读的音素")

    capped_phones = 0
    def cap_duration(module, args, log_frames):
        nonlocal capped_phones
        if log_frames.shape != (1, 1, len(phone_symbols)) or not torch.isfinite(log_frames).all():
            raise ValueError("无效的 Melo 音素时长预测")
        over = log_frames.exp().ceil() > ceilings
        capped_phones = int((over & spoken).sum().item())
        # Melo subsequently does ceil(exp(log_frames)). Leave a fraction of
        # a frame so rounding a capped log-duration cannot add one frame.
        return torch.where(over, (ceilings - 0.25).log(), log_frames)

    with torch.inference_mode(), model.dp.register_forward_hook(cap_duration):
        audio, attention, *_ = model.infer(
            phones[None], torch.tensor([len(phones)], device=device),
            torch.tensor([speaker], device=device), tones[None], lang_ids[None],
            bert[None], ja_bert[None], sdp_ratio=0.0, length_scale=1.0, noise_scale=0.6,
        )
    # Check the frame path actually used by the decoder. A future incompatible
    # engine must not silently bypass the duration limits.
    frames = attention[0, 0].sum(dim=0)
    if frames.shape != ceilings.shape or not torch.all((frames >= 1) & (frames <= ceilings)):
        raise RuntimeError("Melo 解码器分配的音素帧数与时长约束不符")
    timing = {
        "capped_phonemes": capped_phones,
        "max_phoneme_seconds": round(float(frames[spoken].max().item()) * frame_seconds, 5),
    }
    return audio[0, 0], timing


class PacedMandarin:
    def __init__(self, root, device):
        root = Path(root).expanduser().resolve()
        melo_dir = root / "melo"
        converter_dir = root / "converter"
        source_path = root / "base_speakers" / "ses" / "zh.pth"
        for path in [melo_dir / "config.json", melo_dir / "checkpoint.pth",
                     converter_dir / "config.json", converter_dir / "checkpoint.pth", source_path]:
            if not path.is_file():
                raise FileNotFoundError(f"缺少离线中文配音文件: {path}")
        config = json.loads((converter_dir / "config.json").read_text(encoding="utf-8"))
        if config.get("_version_") != "v2":
            raise ValueError("中文时长控制需要 OpenVoice V2 音色转换器")

        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import nltk
        # g2p_en otherwise tries to download these corpora at import time.
        for resource in ("corpora/cmudict.zip", "taggers/averaged_perceptron_tagger.zip"):
            nltk.data.find(resource)
        import torch
        from melo.models import SynthesizerTrn as MeloModel
        from transformers import AutoTokenizer, AutoModelForMaskedLM
        from openvoice.models import SynthesizerTrn

        self.device = torch.device(device)
        self.melo_config = json.loads((melo_dir / "config.json").read_text())
        cfg = self.melo_config
        self.melo = MeloModel(
            len(cfg["symbols"]), cfg["data"]["filter_length"] // 2 + 1,
            cfg["train"]["segment_size"] // cfg["data"]["hop_length"],
            n_speakers=cfg["data"]["n_speakers"], num_tones=cfg["num_tones"],
            num_languages=cfg["num_languages"], **cfg["model"],
        ).to(self.device).eval()
        checkpoint = torch.load(melo_dir / "checkpoint.pth", map_location="cpu", weights_only=False)
        self.melo.load_state_dict(checkpoint["model"], strict=True)
        self.speaker = cfg["data"]["spk2id"]["ZH"]
        self.guide_rate = cfg["data"]["sampling_rate"]
        self.frame_seconds = cfg["data"]["hop_length"] / self.guide_rate
        self.symbol_to_id = {s: i for i, s in enumerate(cfg["symbols"])}
        self.tokenizer = AutoTokenizer.from_pretrained("bert-base-multilingual-uncased")
        self.bert = AutoModelForMaskedLM.from_pretrained(
            "bert-base-multilingual-uncased").to(self.device).eval()

        # Instantiate the acoustic converter directly with explicit local assets.
        self.config = config["data"]
        self.sample_rate = self.config["sampling_rate"]
        self.converter = SynthesizerTrn(
            len(config.get("symbols", [])), self.config["filter_length"] // 2 + 1,
            n_speakers=self.config["n_speakers"], **config["model"],
        ).to(self.device).eval()
        state = torch.load(converter_dir / "checkpoint.pth", map_location="cpu", weights_only=True)
        self.converter.load_state_dict(state["model"], strict=True)
        self.source_embedding = torch.load(source_path, map_location=device, weights_only=True)
        self.last_timing = {}
        self.reference_embeddings = {}

    def _spectrum(self, waveform):
        from openvoice.mel_processing import spectrogram_torch

        return spectrogram_torch(waveform, self.config["filter_length"], self.sample_rate,
                                 self.config["hop_length"], self.config["win_length"], center=False)

    def _text_inputs(self, text):
        import torch
        from melo import commons
        from melo.text import chinese_mix, cleaned_text_to_sequence

        normalized = chinese_mix.text_normalize(text)
        phones, tones, word2ph = chinese_mix.g2p(normalized)
        phones, tones, languages = cleaned_text_to_sequence(
            phones, tones, "ZH_MIX_EN", self.symbol_to_id)
        if self.melo_config["data"]["add_blank"]:
            phones = commons.intersperse(phones, 0)
            tones = commons.intersperse(tones, 0)
            languages = commons.intersperse(languages, 0)
            word2ph = [count * 2 for count in word2ph]
            word2ph[0] += 1
        encoded = self.tokenizer(normalized, return_tensors="pt").to(self.device)
        hidden = self.bert(**encoded, output_hidden_states=True).hidden_states[-3][0]
        if len(hidden) != len(word2ph):
            raise ValueError("中文文本与音素特征未对齐")
        features = torch.repeat_interleave(
            hidden, torch.tensor(word2ph, device=self.device), dim=0).T
        if features.shape[-1] != len(phones):
            raise ValueError("中文音素数量与声学特征不一致")
        return (torch.zeros(1024, len(phones), device=self.device), features,
                torch.tensor(phones, device=self.device), torch.tensor(tones, device=self.device),
                torch.tensor(languages, device=self.device))

    def generate(self, text, reference_path):
        import soundfile as sf
        import torch
        import torchaudio.functional as audio_ops

        started = time.perf_counter()
        self.last_timing = {}
        with torch.inference_mode():
            inputs = self._text_inputs(text)
            guide, timing = _bounded_guide(self.melo, inputs,
                                           self.melo_config["symbols"], self.speaker, self.frame_seconds)
            guide_seconds = guide.numel() / self.guide_rate
            guide = audio_ops.resample(guide, self.guide_rate, self.sample_rate)
            spectrum = self._spectrum(guide[None])
            reference_hash = file_sha256(reference_path)
            if reference_hash not in self.reference_embeddings:
                samples, rate = sf.read(reference_path, dtype="float32", always_2d=True)
                reference = torch.from_numpy(samples.mean(axis=1)).to(self.device)
                reference = audio_ops.resample(reference, rate, self.sample_rate)
                self.reference_embeddings[reference_hash] = self.converter.ref_enc(
                    self._spectrum(reference[None]).transpose(1, 2))
            target_embedding = self.reference_embeddings[reference_hash]
            lengths = torch.tensor([spectrum.shape[-1]], device=self.device)
            converted = self.converter.voice_conversion(
                spectrum, lengths, self.source_embedding, target_embedding.unsqueeze(-1), tau=0.3,
            )[0][0, 0]
            converted_seconds = converted.numel() / self.sample_rate
            # An STFT may discard the final partial hop. Larger drift means
            # this converter is incompatible with a duration-preserving path.
            if abs(converted_seconds - guide_seconds) > (self.config["hop_length"] + 1) / self.sample_rate:
                raise RuntimeError("音色转换输出与中文导读时长不一致")
            self.last_timing = dict(timing, guide_seconds=round(guide_seconds, 5),
                                    converted_seconds=round(converted_seconds, 5),
                                    synthesis_seconds=round(time.perf_counter() - started, 4))
            return converted.float().cpu().numpy()
