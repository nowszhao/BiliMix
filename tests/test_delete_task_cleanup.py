"""
测试 delete_task 是否正确清理关联文件（视频 / ass 字幕 / 提取音频）。

场景：
  1. 本地上传视频任务：删除任务时应删除 data/downloads 下的视频、ass 字幕、
     video_cache 中的提取音频，且不误删无关文件
  2. YouTube 视频任务：删除任务时应删除 video_cache 下的视频、info.json、提取音频
  3. 服务重启后（任务仅存在于 SQLite 索引 + task_result.json）：仍能恢复路径并删除
  4. server-mode 引用 downloads 目录外的路径：不应误删

说明：本测试只注册 task_query 蓝图，不导入 services.web_app，
避免触发 web_app 模块导入时的依赖检测（sys.exit）。
"""

import json
import os
import shutil
import sys
import tempfile
from unittest.mock import patch

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask
from core import config
from core import task_manager as tm
from services.shared import _queue_waiters
from services.task_query import task_query_bp


def _reset_global_state():
    """在每次测试前重置 task_manager 与队列的全局状态"""
    with tm.tasks_lock:
        tm.tasks.clear()
    tm.cancel_flags.clear()
    tm.task_subprocesses.clear()
    _queue_waiters.clear()


class TestDeleteTaskFileCleanup:
    """验证 delete_task 清理视频 / 字幕 / 音频文件"""

    def setup_method(self):
        _reset_global_state()
        # 重定向目录配置到临时目录，避免触碰真实 data/
        self._tmp = tempfile.mkdtemp(prefix="bilimix_test_")
        self.dl = os.path.join(self._tmp, "downloads")
        self.vc = os.path.join(self.dl, "video_cache")
        self.res = os.path.join(self._tmp, "results")
        self.out = os.path.join(self._tmp, "transcripts")
        os.makedirs(self.vc)
        os.makedirs(self.res)
        os.makedirs(self.out)

        self._orig_dirs = (config.DOWNLOAD_DIR, config.RESULT_DIR,
                           config.OUTPUT_DIR, config.BASE_DIR)
        config.DOWNLOAD_DIR = self.dl
        config.RESULT_DIR = self.res
        config.OUTPUT_DIR = self.out
        config.BASE_DIR = self._tmp

        self.app = Flask(__name__)
        self.app.config["TESTING"] = True
        self.app.register_blueprint(task_query_bp)

    def teardown_method(self):
        _reset_global_state()
        (config.DOWNLOAD_DIR, config.RESULT_DIR,
         config.OUTPUT_DIR, config.BASE_DIR) = self._orig_dirs
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _put_task(self, task_id, **kwargs):
        base = {
            "task_id": task_id, "url": "", "title": "t",
            "process_mode": "video", "type": "video", "status": "completed",
            "created_at": "2026-01-01 00:00:00",
            "_basename": "", "_audio_path": "", "_video_path": "",
            "_subtitle_mode": "bilingual", "_subtitle_font_size": 20,
            "_subtitle_path": "", "keep_bgm": False,
        }
        base.update(kwargs)
        with tm.tasks_lock:
            tm.tasks[task_id] = base
        return task_id

    def _delete(self, task_id, index=None):
        if index is None:
            index = {}
        with patch("services.task_query.load_tasks_index", return_value=index), \
             patch("services.task_query.delete_task_from_index"):
            return self.app.test_client().delete(f"/api/task/{task_id}")

    @staticmethod
    def _touch(*paths):
        for p in paths:
            with open(p, "w") as f:
                f.write("x")

    def test_delete_local_video_task_removes_video_ass_and_audio(self):
        """本地上传视频任务：视频、ass 字幕、提取音频都被删除，无关文件保留"""
        video = os.path.join(self.dl, "MyVideo.mp4")
        sub = os.path.join(self.dl, "MyVideo.ass")
        audio = os.path.join(self.vc, "audio_abc123.wav")
        other = os.path.join(self.dl, "other.mp4")
        self._touch(video, sub, audio, other)

        task_id = "v" * 31 + "1"
        self._put_task(task_id, url="file://" + video,
                       _basename="audio_abc123_abcdef12",
                       _audio_path=audio, _video_path=video,
                       _subtitle_path=sub)

        resp = self._delete(task_id)
        assert resp.status_code == 200
        data = resp.get_json()
        assert not os.path.exists(video), "视频文件应被删除"
        assert not os.path.exists(sub), "ass 字幕应被删除"
        assert not os.path.exists(audio), "提取音频应被删除"
        assert os.path.exists(other), "无关文件不应被删除"
        assert "data/downloads/MyVideo.mp4" in data["cleaned_files"]
        assert "data/downloads/MyVideo.ass" in data["cleaned_files"]

    def test_delete_youtube_video_task_removes_cache_and_info_json(self):
        """YouTube 任务：video_cache 视频、info.json、提取音频都被删除"""
        yt_video = os.path.join(self.vc, "9f86d081_title.mp4")
        yt_info = os.path.join(self.vc, "9f86d081_title.info.json")
        yt_audio = os.path.join(self.vc, "audio_md5abc.wav")
        self._touch(yt_video, yt_info, yt_audio)

        task_id = "y" * 31 + "2"
        self._put_task(task_id,
                       url="https://www.youtube.com/watch?v=x",
                       _basename="audio_md5abc_y2y2y2y2",
                       _audio_path=yt_audio, _video_path=yt_video)

        resp = self._delete(task_id)
        assert resp.status_code == 200
        assert not os.path.exists(yt_video), "YouTube 视频应被删除"
        assert not os.path.exists(yt_info), "info.json 应被删除"
        assert not os.path.exists(yt_audio), "提取音频应被删除"

    def test_delete_restored_task_removes_files(self):
        """服务重启后（任务仅存 SQLite 索引 + task_result.json）仍能清理文件"""
        basename = "audio_r3st_r3st0001"
        result_dir = os.path.join(self.res, basename)
        os.makedirs(result_dir, exist_ok=True)

        r_video = os.path.join(self.dl, "Restart.mp4")
        r_sub = os.path.join(self.dl, "Restart.ass")
        r_audio = os.path.join(self.vc, "audio_rest.wav")
        self._touch(r_video, r_sub, r_audio)

        task_id = "r" * 31 + "3"
        with open(os.path.join(result_dir, "task_result.json"), "w") as f:
            json.dump({
                "task_id": task_id, "status": "completed",
                "process_mode": "video",
                "_basename": basename,
                "_video_path": r_video, "_subtitle_path": r_sub,
                "result": {"basename": basename, "original_audio": r_audio},
            }, f)
        index = {task_id: {"basename": basename, "status": "completed",
                           "type": "video"}}

        with patch("core.task_manager.get_task_from_index",
                   return_value=index[task_id]):
            resp = self._delete(task_id, index=index)
        assert resp.status_code == 200
        assert not os.path.exists(r_video), "恢复任务的视频应被删除"
        assert not os.path.exists(r_sub), "恢复任务的字幕应被删除"
        assert not os.path.exists(r_audio), "恢复任务的音频应被删除"

    def test_delete_does_not_remove_files_outside_downloads(self):
        """server-mode 引用 downloads 目录外路径时不应误删文件"""
        sv = os.path.join(self._tmp, "server_video.mp4")
        self._touch(sv)

        task_id = "s" * 31 + "4"
        self._put_task(task_id, url="file://" + sv,
                       _basename="audio_sv_sv0000001",
                       _video_path=sv)

        resp = self._delete(task_id)
        assert resp.status_code == 200
        assert os.path.exists(sv), "downloads 外的文件不应被删除"

    def test_delete_audio_task_does_not_touch_other_tasks_video_cache(self):
        """音频任务即使命名为 audio_xxx，也不应误删 video_cache 中其他任务的提取音频"""
        cache_audio = os.path.join(self.vc, "audio_other.wav")
        self._touch(cache_audio)

        task_id = "a" * 31 + "5"
        self._put_task(task_id, type="audio",
                       process_mode="sentence_translate",
                       _basename="audio_foo",
                       _audio_path=os.path.join(self.dl, "audio_foo.mp3"))

        resp = self._delete(task_id)
        assert resp.status_code == 200
        assert os.path.exists(cache_audio), "其他任务的提取音频不应被删除"
