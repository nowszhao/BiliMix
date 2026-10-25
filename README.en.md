<h1 align="center">BiliMix</h1>

<p align="center"><b>English video / podcasts → Chinese dubbing in the original voice + bilingual subtitles.</b><br>Install one Agent Skill. Give it a link.</p>

<p align="center">
  <a href="#quick-start"><img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="#requirements"><img src="https://img.shields.io/badge/Local_inference-100%25-1b1b19?style=flat-square" alt="Local inference"></a>
  <a href="#requirements"><img src="https://img.shields.io/badge/Platform-macOS_%7C_Linux-1b1b19?style=flat-square" alt="macOS and Linux"></a>
  <a href="skills/nows-ytb-dubbing/SKILL.md"><img src="https://img.shields.io/badge/Agent_Skill-nows--ytb--dubbing-1b1b19?style=flat-square" alt="Agent Skill"></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-1b1b19?style=flat-square" alt="MIT license"></a>
  <a href="https://nowszhao.github.io/BiliMix/"><img src="https://img.shields.io/badge/Website-nowszhao.github.io%2FBiliMix-1b1b19?style=flat-square" alt="Website"></a>
</p>

<p align="center">
  <a href="https://nowszhao.github.io/BiliMix/"><img src="https://img.shields.io/badge/Website-nowszhao.github.io%2FBiliMix-1b1b19?style=for-the-badge" alt="Website"></a>
  &nbsp;
  <a href="https://github.com/nowszhao/BiliMix/archive/refs/heads/main.zip"><img src="https://img.shields.io/badge/Download-ZIP-1b1b19?style=for-the-badge&logo=github&logoColor=white" alt="Download ZIP"></a>
  &nbsp;
  <a href="#quick-start"><img src="https://img.shields.io/badge/Install-Agent_Skill-d7d7d0?style=for-the-badge&labelColor=1b1b19" alt="Install Agent Skill"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#gallery">Gallery</a> ·
  <a href="#what-it-makes">What it makes</a> ·
  <a href="#install">Install</a> ·
  <a href="#usage">Usage</a> ·
  <a href="#examples">Examples</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#requirements">Requirements</a> ·
  <a href="#limits">Limits</a>
</p>

---

BiliMix turns English videos, podcasts, and interviews into Chinese dubbing while preserving the original speaker's voice, picture, and background audio. It also produces bilingual subtitles.

You can make video dubbing, audio dubbing, subtitles only, or standalone speech. The only user-facing entry point is an Agent Skill: it handles downloads, uploads, FULL, translation, task progress, and delivery, while the BiliMix service handles speaker analysis, TTS, mixing, and final assembly.

[中文](README.md)

## Quick start

### 1. Install the Skill

Put [`skills/nows-ytb-dubbing`](skills/nows-ytb-dubbing) into any Agent that supports Skills. You can also use `nows-ytb-dubbing.zip` from a release, or your Agent's own Skill installer.

The Skill bundles the client SDK wheel. On first use, the Agent runs the Skill setup when needed and installs yt-dlp, FFmpeg tools, and the SDK into the project directory without changing the Agent environment.

### 2. Give it a link

Tell the Agent directly:

```text
Use nows-ytb-dubbing to dub this video into Chinese:
https://www.youtube.com/watch?v=...
```

On first use, the Agent checks the BiliMix service. If it can access the target machine and the service is not ready, it prepares the server from the installation guide. After that, every task only needs a link.

## Gallery

Original on the left, Chinese dubbing on the right. Click either video to play.

<table>
  <tr>
    <td width="50%"><video src="examples/test1.mp4" poster="examples/screenshot_original.jpg" controls preload="metadata" playsinline width="100%"><a href="examples/test1.mp4"><img src="examples/screenshot_original.jpg" width="100%" alt="test1 original"></a></video></td>
    <td width="50%"><video src="examples/test1_mixed.mp4" poster="examples/screenshot_mixed.jpg" controls preload="metadata" playsinline width="100%"><a href="examples/test1_mixed.mp4"><img src="examples/screenshot_mixed.jpg" width="100%" alt="test1 Chinese dubbing"></a></video></td>
  </tr>
  <tr>
    <td><sub><b>Before</b>: original English audio.</sub></td>
    <td><sub><b>After</b>: Chinese dubbing in the original voice + bilingual subtitles.</sub></td>
  </tr>
</table>

If your browser does not support embedded video, open [test1 original](examples/test1.mp4) or [test1 Chinese dubbing](examples/test1_mixed.mp4).

## What it makes

| Kind | What it is | Output |
|---|---|---|
| **Video dubbing** | Translate an English video sentence by sentence and read the Chinese in the original speaker's voice | Chinese-dubbed MP4 + bilingual ASS |
| **Audio dubbing** | English podcasts, interviews, or audio files | Chinese-dubbed MP3 |
| **Subtitles only** | Run FULL, translation, and subtitle layout without connecting to the BiliMix server | Bilingual ASS |
| **Standalone TTS** | Reference audio plus text | Speech in the reference voice |

Subtitles place Chinese above English by default, allow at most two lines per language, and keep text height within 25% of the frame. Subtitle pagination is independent of TTS sentence boundaries.

## Install

### Skill

The Skill is the only entry point users need to install. It contains:

- `SKILL.md`: instructions for the Agent
- `scripts/workflow.py`: download, upload, validation, task progress, and delivery
- `assets/`: bundled SDK wheel

Installation depends on your Agent: use its Skill installer, or place `skills/nows-ytb-dubbing` in its skills directory. Any Agent that can read files and run commands can use it.

### BiliMix service

The BiliMix service handles speaker analysis, TTS, mixing, and video assembly. It can run on the local machine or on a reachable machine.

For first use, ask the Agent to prepare the service following [Installation and configuration](docs/installation.md). If a BiliMix service is already running, tell the Agent its address.

## Usage

### First use

```text
Use nows-ytb-dubbing to prepare the BiliMix service, then dub this video into Chinese:
https://www.youtube.com/watch?v=...
```

The Agent handles environment checks, service preparation, upload, speaker analysis, FULL, translation, TTS, mixing, and download. You do not need to run the CLI by hand.

### Later use

```text
Use nows-ytb-dubbing to dub this video into Chinese:
https://www.youtube.com/watch?v=...
```

If the service is already running, just provide the link. The Agent asks for your input only when needed:

- Overlapping speech or a speaker boundary needs a listening review
- No qualified same-speaker reference is available
- A server task fails or needs recovery

## Examples

| Example | What it shows | Open |
|---|---|---|
| `demo` | 60-second original and Chinese dubbing side by side | [demo.html](demo.html) · [MP4](examples/demo-dubbed.mp4) |
| `test1` | Longer English input and its fully dubbed version | [Original](examples/test1.mp4) · [Dubbed](examples/test1_mixed.mp4) |
| `subtitle-only` | Bilingual subtitles only, without the BiliMix server | [Subtitle guide](skills/nows-ytb-dubbing/references/subtitles.md) |

## How it works

1. **Agent language work**: FULL preserves every source word and its order; translation produces complete Chinese utterances and stable source-word mappings.
2. **BiliMix speaker and speech**: WhisperX word timing, speaker diarization, acoustic verification, reference selection, and Confucius4-TTS synthesis.
3. **Final assembly**: mix the audio, retime the picture to the Chinese audio, burn in bilingual subtitles, and return MP4, MP3, ASS, and a reference report.

Analysis and dubbing tasks share audio and Demucs caches keyed by media SHA256. Each video is extracted once and separated once.

Subtitle pagination is independent of TTS boundaries. One complete Chinese utterance maps to one TTS call; display subtitles can be paginated without splitting the dubbing.

## Requirements

- **Server**: Python 3.10–3.12, FFmpeg 7+, FFprobe, CJK fonts, WhisperX, pyannote, Demucs, and Confucius4-TTS
- **Client**: Python 3.10+, the SDK; URL download also needs `yt-dlp` and FFmpeg
- **Hardware**: minimum 4 CPU cores, 8 GB RAM, 15 GB disk; 8 cores and 16 GB+ recommended
- **GPU**: optional, for faster TTS and WhisperX
- **Speaker models**: set `HF_TOKEN` when Hugging Face access is required

## Limits

- CPU inference is the main bottleneck. Demucs, speaker diarization, and TTS can take a long time.
- Speaker analysis is probabilistic. Overlap, speaker changes, and unknown voices need human review; the system does not guess.
- External refined dubbing requires a work ASS, a manifest, and completed speaker analysis. Missing inputs fail explicitly instead of silently falling back to server translation.
- The current deployment uses one process and one shared heavy-task execution slot. Do not add WSGI workers to scale model concurrency.
- Reference audio quality determines cloning quality. Without a qualified same-speaker reference, the system does not borrow another voice.

## License

MIT

<p align="center"><sub>If BiliMix helps you, a star helps others find it.</sub></p>
