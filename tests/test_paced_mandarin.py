"""Exercise duration limits at the decoder seam without loading model weights."""
import pytest

torch = pytest.importorskip("torch")

from workers.paced_mandarin import _bounded_guide


class DurationPredictor(torch.nn.Module):
    def __init__(self, frames):
        super().__init__()
        self.register_buffer("log_frames", torch.tensor(frames, dtype=torch.float32).log()[None, None])

    def forward(self, *args):
        return self.log_frames


class AcousticModel:
    def __init__(self, frames):
        self.dp = DurationPredictor(frames)
        self.duration_calls = 0
        self.frames = None

    def infer(self, phones, lengths, sid, *args, sdp_ratio, length_scale, **kwargs):
        assert sdp_ratio == 0.0
        assert length_scale == 1.0
        self.duration_calls += 1
        # Melo exposes the decoded monotonic alignment, one column per phone.
        self.frames = self.dp(None).exp().ceil().long().view(-1)
        attention = torch.repeat_interleave(torch.eye(len(self.frames)), self.frames, dim=0)
        audio = torch.repeat_interleave(phones[0].float(), self.frames)[None, None]
        return audio, attention[None, None], None


def inputs(phones):
    ids = torch.tensor(phones, dtype=torch.int64)
    features = torch.zeros(2, len(phones))
    return features, features, ids, ids, ids


def test_long_local_phoneme_is_bounded_before_waveform_generation():
    model = AcousticModel([7, 120, 6, 55])
    audio, timing = _bounded_guide(model, inputs([1, 2, 0, 3]), ["_", "h", "ao", ","], 1, .01)
    assert model.duration_calls == 1
    assert model.frames[:3].tolist() == [7, 32, 6]
    # Stock ceil(exp(log(n))) may round a mathematically integral count up.
    assert model.frames[3] == model.dp(None).exp().ceil().long().view(-1)[3]
    assert len(audio) == model.frames.sum()
    assert timing == {"capped_phonemes": 1, "max_phoneme_seconds": .32}
    assert not model.dp._forward_hooks


def test_valid_pronunciation_and_punctuation_keep_their_predicted_frames():
    model = AcousticModel([10, 23, 6, 51])
    baseline = model.dp(None).exp().ceil().long()
    _, timing = _bounded_guide(model, inputs([1, 2, 0, 3]), ["_", "h", "ao", "."], 1, .01)
    torch.testing.assert_close(model.frames, baseline.view(-1))
    assert timing["capped_phonemes"] == 0


def test_hook_is_removed_when_decoder_raises():
    model = AcousticModel([100])
    def fail(*args, **kwargs):
        raise RuntimeError("decoder failed")
    model.infer = fail
    with pytest.raises(RuntimeError, match="decoder failed"):
        _bounded_guide(model, inputs([0]), ["a"], 1, .01)
    assert not model.dp._forward_hooks


def test_incompatible_decoder_cannot_bypass_limits():
    model = AcousticModel([100])
    def bypass(*args, **kwargs):
        return torch.zeros(1, 1, 100), torch.ones(1, 1, 100, 1), None
    model.infer = bypass
    with pytest.raises(RuntimeError, match="时长约束"):
        _bounded_guide(model, inputs([0]), ["a"], 1, .01)
