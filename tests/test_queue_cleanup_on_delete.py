"""
测试 delete_task 是否正确清理 _queue_waiters，防止排队死锁。

场景：
  1. 任务 A 在运行，任务 B 在排队
  2. 删除排队中的任务 B
  3. 验证 _queue_waiters 中不再有 B，后续任务不会死锁
"""

import sys
import os
import threading
import time
from unittest.mock import patch, MagicMock

# 确保项目根在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# Fixtures: 隔离副作用
# ============================================================

ORIGINAL_TASKS = {}
ORIGINAL_CANCEL_FLAGS = {}
ORIGINAL_SUBPROCESSES = {}
ORIGINAL_WAITERS = None


def setup_module():
    """保存模块加载前的全局状态（这些会在 import web_app 时被模块级代码初始化）"""
    pass


def _reset_global_state():
    """在每次测试前重置 web_app 和 task_manager 的全局状态"""
    from services import web_app as wa
    from core import task_manager as tm

    # 清空队列
    with wa._queue_condition:
        wa._queue_waiters.clear()

    # 清空任务
    with tm.tasks_lock:
        tm.tasks.clear()
    tm.cancel_flags.clear()
    tm.task_subprocesses.clear()


# ============================================================
# 辅助：模拟提交一个任务到队列
# ============================================================

def _create_mock_task(task_id, task_type="video", status="queued", source_url="file:///test/video.mp4"):
    """在内存中创建一个模拟任务，并加入 _queue_waiters"""
    from core import task_manager as tm

    with tm.tasks_lock:
        tm.tasks[task_id] = {
            "task_id": task_id,
            "url": source_url,
            "title": f"Test {task_id[:8]}",
            "process_mode": "video" if task_type == "video" else "sentence_translate",
            "type": task_type,
            "status": status,
            "step": "",
            "progress": 0,
            "message": "",
            "created_at": "2026-01-01 00:00:00",
            "original_duration": 0,
            "transcription_text": "",
            "segments": [],
            "difficult_words": [],
            "replacements": [],
            "translations": {},
            "translated_indices": [],
            "result": None,
            "video_result": None,
            "_basename": "",
            "_audio_path": "",
            "_video_path": "",
            "_subtitle_mode": "bilingual",
            "_subtitle_font_size": 20,
            "keep_bgm": False,
        }

    tm.cancel_flags[task_id] = threading.Event()


def _enqueue_task(task_id):
    """将一个任务加入排队队列"""
    from services import web_app as wa
    with wa._queue_condition:
        wa._queue_waiters.append(task_id)


# ============================================================
# 测试用例
# ============================================================

class TestDeleteTaskQueueCleanup:
    """验证 delete_task 正确清理 _queue_waiters"""

    def setup_method(self):
        _reset_global_state()
        from services import web_app as wa
        self.app = wa.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def teardown_method(self):
        _reset_global_state()

    @patch("services.web_app.setup_database")
    @patch("services.web_app.load_tasks_index", return_value={})
    @patch("services.web_app.delete_task_from_index")
    def test_delete_queued_task_removes_from_waiters(
        self, mock_delete_index, mock_load_index, mock_setup_db
    ):
        """删除排队中的任务后，_queue_waiters 不应再包含该任务"""
        from services import web_app as wa

        task_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
        task_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"

        # 任务 A 正在运行，任务 B 在排队
        _create_mock_task(task_a, status="processing")
        _create_mock_task(task_b, status="queued")

        # 只把 B 放入排队队列（A 已经在运行，不在队列中）
        _enqueue_task(task_b)

        assert task_b in wa._queue_waiters, "B 应该在排队队列中"
        assert len(wa._queue_waiters) == 1

        # 删除任务 B
        resp = self.client.delete(f"/api/task/{task_b}")
        assert resp.status_code == 200

        # 验证 B 已从排队队列中移除
        with wa._queue_condition:
            assert task_b not in wa._queue_waiters, "删除后 B 不应该还在排队队列中"

        # 验证 B 已从任务字典中移除
        from core import task_manager as tm
        with tm.tasks_lock:
            assert task_b not in tm.tasks

    @patch("services.web_app.setup_database")
    @patch("services.web_app.load_tasks_index", return_value={})
    @patch("services.web_app.delete_task_from_index")
    def test_delete_queued_task_does_not_block_subsequent_tasks(
        self, mock_delete_index, mock_load_index, mock_setup_db
    ):
        """删除排在队首的排队任务后，后续任务应该能正常出队"""
        from services import web_app as wa

        task_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
        task_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"
        task_c = "ccccccccccccccccccccccccccccccc3"

        # 任务 A 正在运行（不在队列），任务 B 和 C 在排队
        _create_mock_task(task_a, status="processing")
        _create_mock_task(task_b, status="queued")
        _create_mock_task(task_c, status="queued")

        _enqueue_task(task_b)
        _enqueue_task(task_c)

        assert list(wa._queue_waiters) == [task_b, task_c]

        # 删除排在队首的 B
        resp = self.client.delete(f"/api/task/{task_b}")
        assert resp.status_code == 200

        # 验证队列只剩下 C
        with wa._queue_condition:
            assert list(wa._queue_waiters) == [task_c], \
                f"删除 B 后队列应该只剩 C，实际: {list(wa._queue_waiters)}"

    @patch("services.web_app.setup_database")
    @patch("services.web_app.load_tasks_index", return_value={})
    @patch("services.web_app.delete_task_from_index")
    def test_delete_non_queued_task_does_not_affect_queue(
        self, mock_delete_index, mock_load_index, mock_setup_db
    ):
        """删除一个不在排队中的任务（如已完成任务）不应影响队列"""
        from services import web_app as wa

        task_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
        task_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"

        _create_mock_task(task_a, status="completed")
        _create_mock_task(task_b, status="queued")

        # B 在排队
        _enqueue_task(task_b)

        assert list(wa._queue_waiters) == [task_b]

        # 删除已完成的 A（不在队列中）
        resp = self.client.delete(f"/api/task/{task_a}")
        assert resp.status_code == 200

        # 队列不应受影响
        with wa._queue_condition:
            assert list(wa._queue_waiters) == [task_b], \
                "删除非排队任务不应该影响队列"

    @patch("services.web_app.setup_database")
    @patch("services.web_app.load_tasks_index", return_value={})
    @patch("services.web_app.delete_task_from_index")
    def test_delete_queued_task_notifies_other_waiters(
        self, mock_delete_index, mock_load_index, mock_setup_db
    ):
        """删除排队任务后 _queue_condition.notify_all 应被调用"""
        from services import web_app as wa

        task_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
        task_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"

        _create_mock_task(task_a, status="processing")
        _create_mock_task(task_b, status="queued")

        _enqueue_task(task_b)

        # 记录 notify_all 是否被调用
        notify_called = []

        def fake_notify_all():
            notify_called.append(True)

        original_condition = wa._queue_condition
        with patch.object(original_condition, "notify_all", side_effect=fake_notify_all):
            resp = self.client.delete(f"/api/task/{task_b}")
            assert resp.status_code == 200

        assert len(notify_called) > 0, \
            "delete 应该调用 _queue_condition.notify_all() 唤醒其他等待者"


class TestQueueDeadlockRegression:
    """回归测试：模拟完整的死锁场景"""

    def setup_method(self):
        _reset_global_state()
        from services import web_app as wa
        self.app = wa.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def teardown_method(self):
        _reset_global_state()

    @patch("services.web_app.setup_database")
    @patch("services.web_app.load_tasks_index", return_value={})
    @patch("services.web_app.delete_task_from_index")
    def test_full_deadlock_scenario(
        self, mock_delete_index, mock_load_index, mock_setup_db
    ):
        """
        完整死锁场景：
        1. 任务 A 在运行，B 排队
        2. 用户删除 B（通过 API）
        3. 提交新任务 C
        4. A 完成后，C 应该能正常出队（不会因为 B 的幽灵而卡死）
        """
        from services import web_app as wa
        from core import task_manager as tm

        task_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
        task_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"
        task_c = "ccccccccccccccccccccccccccccccc3"

        # Step 1: A 运行中，B 排队
        _create_mock_task(task_a, status="processing")
        _create_mock_task(task_b, status="queued")
        _enqueue_task(task_b)

        assert list(wa._queue_waiters) == [task_b]

        # Step 2: 删除 B
        resp = self.client.delete(f"/api/task/{task_b}")
        assert resp.status_code == 200

        # B 应该已经从队列中移除
        with wa._queue_condition:
            assert task_b not in wa._queue_waiters

        # Step 3: 提交新任务 C
        _create_mock_task(task_c, status="queued")
        _enqueue_task(task_c)

        with wa._queue_condition:
            assert task_c in wa._queue_waiters, "C 应该在排队队列中"
            assert task_b not in wa._queue_waiters, "B 的幽灵不应该还在队列中"

        # Step 4: 模拟 A 完成后的出队逻辑
        # 用一个新线程模拟 worker 的排队逻辑
        acquired = threading.Event()

        def simulate_worker():
            with wa._queue_condition:
                # 等待成为队首
                while len(wa._queue_waiters) > 0 and wa._queue_waiters[0] != task_c:
                    wa._queue_condition.wait(timeout=0.1)
                    # 检查是否被取消（防止无限等待）
                    if task_c not in wa._queue_waiters:
                        return
                # C 成为队首了
                acquired.set()
                wa._queue_waiters.popleft()
                wa._queue_condition.notify_all()

        t = threading.Thread(target=simulate_worker, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert acquired.is_set(), \
            "C 应该能够正常出队，不应该因为已删除的 B 而永远卡死"


class TestDeleteRunningTaskQueueCleanup:
    """验证删除正在运行的任务时，队列也能正确清理"""

    def setup_method(self):
        _reset_global_state()
        from services import web_app as wa
        self.app = wa.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def teardown_method(self):
        _reset_global_state()

    @patch("services.web_app.setup_database")
    @patch("services.web_app.load_tasks_index", return_value={})
    @patch("services.web_app.delete_task_from_index")
    def test_delete_processing_task_in_queue(
        self, mock_delete_index, mock_load_index, mock_setup_db
    ):
        """删除一个同时在运行和排队中的任务（边缘情况）"""
        from services import web_app as wa

        task_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"

        _create_mock_task(task_a, status="processing")
        _enqueue_task(task_a)  # 异常情况：同时在运行和排队

        resp = self.client.delete(f"/api/task/{task_a}")
        assert resp.status_code == 200

        with wa._queue_condition:
            assert task_a not in wa._queue_waiters, \
                "即使任务在运行中，也应该从队列中移除"
