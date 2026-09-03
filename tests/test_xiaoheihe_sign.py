"""小黑盒 hkey 签名回归测试（向量来自 Node 对照实现实测）。"""

from __future__ import annotations

import unittest

from manga_uploader.publishers.xiaoheihe import _hkey


class HkeyTest(unittest.TestCase):
    def test_hkey_matches_reference_vectors(self):
        # (path, timestamp, nonce) -> hkey，与 Node 版算法逐字节比对生成
        vectors = [
            (
                "/bbs/app/profile/post/limits",
                1788438096,
                "E07AA7E8F8A630F08E2557E763E8CBAD",
                "U22TV81",
            ),
            (
                "/bbs/app/api/qcloud/cos/upload/info/v2",
                1788437273,
                "6DBF0E32310FCECD82E03F5DD216D186",
                "IXI3169",
            ),
            (
                "/bbs/app/feeds",
                1788437159,
                "896818715EF887A51BF7DEEE3339D02B",
                "DYD7P31",
            ),
        ]
        for path, ts, nonce, expected in vectors:
            with self.subTest(path=path):
                self.assertEqual(_hkey(path, ts, nonce), expected)


if __name__ == "__main__":
    unittest.main()
