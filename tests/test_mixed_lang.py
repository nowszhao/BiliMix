"""
中英混排语种切分（pipeline/mixed_lang.py）的测试。

背景：Confucius4-TTS 是单语种推理，一段里中英混排会共用一套发音规则，
导致中文被英文腔带跑。这里验证「按语种切分 -> 分段合成 -> 拼回一条」的
前半段（切分与拼接）逻辑。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import mixed_lang
from pipeline import tts_direct


# ============================================================
# 1. 语种切分
# ============================================================

class TestSplitLanguageRuns:

    def test_basic_mixed_sentence(self):
        runs = mixed_lang.split_language_runs("I am very 荣幸 to be here.")
        assert [r["lang"] for r in runs] == ["en", "zh", "en"]
        assert runs[0]["text"] == "I am very"
        assert runs[1]["text"] == "荣幸"
        assert runs[2]["text"] == "to be here."

    def test_pure_english(self):
        runs = mixed_lang.split_language_runs("Nothing big. Just three stories.")
        assert len(runs) == 1
        assert runs[0]["lang"] == "en"

    def test_pure_chinese(self):
        runs = mixed_lang.split_language_runs("你好，这是中文。")
        assert len(runs) == 1
        assert runs[0]["lang"] == "zh"

    def test_punctuation_follows_previous_run(self):
        runs = mixed_lang.split_language_runs("hello, 世界！ ok")
        # 逗号跟随英文、感叹号跟随中文
        assert runs[0]["lang"] == "en" and runs[0]["text"] == "hello,"
        assert runs[1]["lang"] == "zh" and "世界" in runs[1]["text"]

    def test_single_char_merged_into_neighbour(self):
        """单个中文夹在英文里时并入相邻片段，避免孤立单字合成"""
        runs = mixed_lang.split_language_runs("hello 好 world")
        assert len(runs) == 1
        assert runs[0]["lang"] == "en"
        assert "好" in runs[0]["text"]

    def test_punct_only_runs_dropped(self):
        runs = mixed_lang.split_language_runs("好，。！")
        assert len(runs) == 1
        assert runs[0]["lang"] == "zh"

    def test_empty_and_blank(self):
        assert mixed_lang.split_language_runs("") == []
        assert mixed_lang.split_language_runs("   ") == []
        assert mixed_lang.split_language_runs("!!!") == []

    def test_adjacent_short_merge_keeps_single_run(self):
        """全是超短片段时最终合并成一段，内容不丢"""
        runs = mixed_lang.split_language_runs("a 好 b")
        assert len(runs) == 1
        assert runs[0]["lang"] in ("en", "zh")
        for token in ("a", "好", "b"):
            assert token in runs[0]["text"]


# ============================================================
# 2. 合成清单
# ============================================================

class TestBuildSpeechPlan:

    def test_auto_splits_and_groups(self):
        texts = ["I am very 荣幸 to be here.", "Nothing big."]
        items, groups = mixed_lang.build_speech_plan(texts, "auto")
        assert len(items) == 4          # 第一段拆 3 片，第二段 1 片
        assert groups[0] == [0, 1, 2]
        assert groups[1] == [3]
        assert [items[i]["lang"] for i in groups[0]] == ["en", "zh", "en"]
        # group 指回原文本下标
        assert items[3]["group"] == 1
        assert items[3]["text"] == "Nothing big."

    def test_fixed_lang_no_split(self):
        texts = ["I am very 荣幸 to be here."]
        items, groups = mixed_lang.build_speech_plan(texts, "zh")
        assert len(items) == 1
        assert items[0]["lang"] == "zh"
        assert groups[0] == [0]

    def test_validate_lang_accepts_auto(self):
        assert tts_direct.validate_lang("auto") == "auto"
        assert tts_direct.validate_lang("AUTO") == "auto"
        with pytest.raises(ValueError):
            tts_direct.validate_lang("fr")


# ============================================================
# 3. 音频拼接
# ============================================================

class TestSpliceWavs:

    def test_splice_concatenates_and_trims_silence(self, tmp_path):
        np = pytest.importorskip("numpy")
        sf = pytest.importorskip("soundfile")

        sr = 22050

        def _make_wav(path, tone_sec, pad_sec=0.1):
            """模拟引擎输出：首尾各 100ms 静音 pad + 中间一段正弦"""
            pad = np.zeros(int(sr * pad_sec), dtype="float32")
            t = np.arange(int(sr * tone_sec), dtype="float32") / sr
            tone = (0.5 * np.sin(2 * np.pi * 220 * t)).astype("float32")
            sf.write(path, np.concatenate([pad, tone, pad]), sr)

        a = str(tmp_path / "a.wav")
        b = str(tmp_path / "b.wav")
        _make_wav(a, 0.3)
        _make_wav(b, 0.3)

        out = str(tmp_path / "merged.wav")
        mixed_lang.splice_wavs([a, b], out, crossfade_ms=25)

        data, out_sr = sf.read(out, dtype="float32")
        assert out_sr == sr
        # 裁剪掉中间静音后应明显短于「两段原始长度之和」
        raw_total = 2 * (0.3 + 0.2)
        assert len(data) / sr < raw_total
        # 但两段语音本身要在
        assert len(data) / sr > 0.5
        assert float(np.max(np.abs(data))) > 0.1

    def test_splice_skips_missing_files(self, tmp_path):
        np = pytest.importorskip("numpy")
        sf = pytest.importorskip("soundfile")

        sr = 22050
        good = str(tmp_path / "good.wav")
        tone = np.sin(2 * np.pi * 300 * np.arange(sr // 2) / sr).astype("float32")
        sf.write(good, tone, sr)

        out = str(tmp_path / "out.wav")
        mixed_lang.splice_wavs([good, str(tmp_path / "nope.wav")], out)
        assert os.path.isfile(out)

    def test_splice_without_any_part_raises(self, tmp_path):
        pytest.importorskip("soundfile")
        with pytest.raises(ValueError):
            mixed_lang.splice_wavs([], str(tmp_path / "out.wav"))
