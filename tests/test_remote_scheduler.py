"""云端定时服务：发布时间校验（选到过去的时间不该“立刻发出去”而是报错）。"""

import tempfile
import time
import unittest
from pathlib import Path

from manga_uploader.remote_scheduler import JobStore


class PublishAtGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _payload(self, publish_at):
        return {
            "config": {},
            "platforms": ["tieba"],
            "chapters": None,
            "publish_at": publish_at,
            "title": "t",
        }

    def test_reject_past_epoch(self):
        with self.assertRaises(ValueError) as ctx:
            self.store.create(self._payload(time.time() - 3600))
        self.assertIn("已经过去", str(ctx.exception))

    def test_reject_past_iso(self):
        with self.assertRaises(ValueError):
            self.store.create(self._payload("2020-01-01T10:00"))

    def test_rejected_job_leaves_nothing_on_disk(self):
        """校验失败的任务不该在服务器上留下配置副本 / 空目录。"""
        with self.assertRaises(ValueError):
            self.store.create(self._payload(time.time() - 7200))
        with self.assertRaises(ValueError):
            self.store.create({"config": {}, "platforms": [], "publish_at": time.time() + 60})
        self.assertEqual(list((Path(self.tmp.name) / "jobs").iterdir()), [])

    def test_accept_future_and_tiny_grace(self):
        job = self.store.create(self._payload(time.time() + 600))
        self.assertEqual(job["status"], "staging")
        self.assertGreater(job["publish_at"], time.time())
        # 宽限期内（刚过去几秒）仍接受，等同“立即发布”
        self.assertTrue(self.store.create(self._payload(time.time() - 5))["id"])
        # 网页 datetime-local 的本地时间字符串（无时区，按北京时间解释）
        self.assertTrue(self.store.create(self._payload("2030-01-01T10:00"))["id"])

    def test_accepts_epoch_seconds_as_string(self):
        """网页端改成提交绝对时间戳（秒）后，字符串形式的时间戳也要认。"""
        when = time.time() + 900
        job = self.store.create(self._payload(str(int(when))))
        self.assertAlmostEqual(job["publish_at"], float(int(when)), places=3)

    def test_publish_at_text_has_timezone_offset(self):
        """任务里显示的时间要带 UTC 偏移，避免“服务器时间对不上”的误会。"""
        job = self.store.create(self._payload(time.time() + 3600))
        self.assertIn("UTC", job["publish_at_text"])

    def test_create_keeps_account_snapshot(self):
        """任务里要留下“创建时用的账号”，方便用户核对（空值不记）。"""
        payload = self._payload(time.time() + 600)
        payload["accounts"] = {"tieba": "hakre（uid 5504679593）", "bilibili": "", "bogus": "  "}
        job = self.store.create(payload)
        self.assertEqual(job["accounts"], {"tieba": "hakre（uid 5504679593）"})

    def test_immediate_job_is_labelled(self):
        """马上要发的任务在列表里要标明“立即发布”，别让人以为是定时。"""
        job = self.store.create(self._payload(time.time() + 5))
        self.assertIn("立即发布", job["publish_at_text"])


class LocalProxyPublishAtGuardTest(unittest.TestCase):
    """本机网页端（web.py）在把任务转给云端之前也要拦一次过去时间。"""

    def test_reject_past_epoch_and_iso(self):
        from manga_uploader.web import _check_future_publish_at

        with self.assertRaises(ValueError):
            _check_future_publish_at(str(int(time.time()) - 3600))
        with self.assertRaises(ValueError):
            _check_future_publish_at("2020-01-01T10:00")
        with self.assertRaises(ValueError):
            _check_future_publish_at("")

    def test_accept_future(self):
        from manga_uploader.web import _check_future_publish_at

        self.assertTrue(_check_future_publish_at(str(int(time.time()) + 600)))
        self.assertTrue(_check_future_publish_at("2030-01-01T10:00"))


if __name__ == "__main__":
    unittest.main()
