#!/usr/bin/env python3
"""
Confucius4-TTS-CPU Worker — 批量句子级 TTS 合成

加载 ConfuciusTTS 模型一次，按任务列表逐句合成中文语音。
每句使用对应的参考音频进行零样本声音克隆。

输入: JSON 任务文件，包含 model 路径、jobs 列表
输出: JSON 结果，包含 success/failed 计数和每条结果的 output_path

用法:
    python workers/confucius_tts_worker.py <task_file.json>
"""
import json
import os
import sys
import time
import traceback

import soundfile as sf
import torch

# 将 Confucius4-TTS-CPU 项目根目录加入 sys.path
_CONFUCIUS_ROOT = os.environ.get(
    "CONFUCIUS4_TTS_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "..", "Confucius4-TTS-CPU"),
)
_CONFUCIUS_ROOT = os.path.abspath(_CONFUCIUS_ROOT)
if _CONFUCIUS_ROOT not in sys.path:
    sys.path.insert(0, _CONFUCIUS_ROOT)

from confuciustts.cli.inference import ConfuciusTTS


def main():
    if len(sys.argv) < 2:
        print("用法: python confucius_tts_worker.py <task_file.json>", file=sys.stderr)
        sys.exit(1)

    task_file = os.path.abspath(sys.argv[1])
    with open(task_file, "r", encoding="utf-8") as f:
        task = json.load(f)

    config_path = task.get("config_path",
                          os.path.join(_CONFUCIUS_ROOT, "config", "inference_config.yaml"))
    config_path = os.path.abspath(config_path)
    device = task.get("device", "cpu")
    jobs = task.get("jobs", [])

    if not jobs:
        print(json.dumps({"success": 0, "failed": 0, "results": []}))
        return

    # 将所有路径转为绝对路径（模型加载后会 chdir）
    for job in jobs:
        if "ref_audio" in job and job["ref_audio"]:
            job["ref_audio"] = os.path.abspath(job["ref_audio"])
        if "output_path" in job and job["output_path"]:
            job["output_path"] = os.path.abspath(job["output_path"])

    # ---- 加载模型 ----
    # 切换到 Confucius4-TTS-CPU 项目根目录（config 中使用相对路径）
    original_cwd = os.getcwd()
    os.chdir(_CONFUCIUS_ROOT)
    t0 = time.time()
    print(f"[ConfuciusWorker] 加载模型... device={device}", flush=True)
    print(f"[ConfuciusWorker] config_path={config_path}", flush=True)
    print(f"[ConfuciusWorker] cwd={os.getcwd()}", flush=True)

    try:
        model = ConfuciusTTS(
            config_path=config_path,
            device=device,
        )
    except Exception as e:
        print(f"[ConfuciusWorker] 模型加载失败: {e}", file=sys.stderr)
        traceback.print_exc()
        os.chdir(original_cwd)
        results = [{"output_path": job.get("output_path", ""),
                     "error": f"模型加载失败: {e}"} for job in jobs]
        print(json.dumps({"success": 0, "failed": len(jobs), "results": results}))
        sys.exit(1)

    load_time = time.time() - t0
    print(f"[ConfuciusWorker] 模型加载完成 ({load_time:.1f}s)", flush=True)

    # ---- 逐句合成 ----
    total = len(jobs)
    results = []
    success = 0
    failed = 0

    gen_kwargs = {
        "temperature": task.get("temperature", 0.8),
        "top_p": task.get("top_p", 0.8),
        "top_k": task.get("top_k", 30),
        "num_beams": task.get("num_beams", 3),
        "repetition_penalty": task.get("repetition_penalty", 10.0),
        "n_timesteps": task.get("n_timesteps", 25),
        "inference_cfg_rate": task.get("inference_cfg_rate", 0.7),
        "verbose": task.get("verbose", False),
    }

    try:
        for i, job in enumerate(jobs):
            text = job.get("text", "")
            ref_audio = job.get("ref_audio", "")
            output_path = job.get("output_path", "")

            if not text or not ref_audio or not output_path:
                print(f"[ConfuciusWorker] [{i+1}/{total}] 跳过: 参数不完整", flush=True)
                results.append({
                    "output_path": output_path,
                    "error": "参数不完整 (text/ref_audio/output_path)",
                })
                failed += 1
                continue

            if not os.path.isfile(ref_audio):
                print(f"[ConfuciusWorker] [{i+1}/{total}] 跳过: 参考音频不存在 {ref_audio}", flush=True)
                results.append({
                    "output_path": output_path,
                    "error": f"参考音频不存在: {ref_audio}",
                })
                failed += 1
                continue

            # 检查缓存
            if os.path.exists(output_path):
                print(f"[ConfuciusWorker] [{i+1}/{total}] 缓存: {text[:30]}...", flush=True)
                results.append({"output_path": output_path, "error": None})
                success += 1
                continue

            t_start = time.time()
            print(f"[ConfuciusWorker] [{i+1}/{total}] 合成: {text[:30]}... "
                  f"(ref: {os.path.basename(ref_audio)})", flush=True)

            try:
                audio = model.generate(
                    text=text,
                    lang="zh",
                    prompt_wav=ref_audio,
                    **gen_kwargs,
                )
                # audio: torch.Tensor shape (1, T_audio)
                audio_np = audio.cpu().squeeze(0).numpy()
                os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                sf.write(output_path, audio_np, model.sample_rate)
                elapsed = time.time() - t_start
                print(f"[ConfuciusWorker] [{i+1}/{total}] 完成 ({elapsed:.1f}s) "
                      f"-> {os.path.basename(output_path)}", flush=True)
                results.append({"output_path": output_path, "error": None})
                success += 1

            except Exception as e:
                elapsed = time.time() - t_start
                print(f"[ConfuciusWorker] [{i+1}/{total}] 失败 ({elapsed:.1f}s): {e}", flush=True)
                traceback.print_exc()
                results.append({"output_path": output_path, "error": str(e)})
                failed += 1
    finally:
        # 恢复原始工作目录
        os.chdir(original_cwd)

    # ---- 输出结果 JSON ----
    summary = {
        "success": success,
        "failed": failed,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False))
    print(f"[ConfuciusWorker] 全部完成: {success} 成功, {failed} 失败", flush=True)


if __name__ == "__main__":
    main()
