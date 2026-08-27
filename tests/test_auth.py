# -*- coding: utf-8 -*-
"""auth 模块单元测试：密码哈希 / 注册 / 登录 / JWT 签发与校验。

运行：python -m unittest tests.test_auth -v
依赖：fastapi + PyJWT + pydantic（缺失时测试自动跳过）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import auth
    HAS_DEPS = True
    IMPORT_ERROR = ""
except ImportError as e:
    auth = None
    HAS_DEPS = False
    IMPORT_ERROR = str(e)


@unittest.skipUnless(HAS_DEPS, f"依赖未安装（fastapi/PyJWT/pydantic），跳过：{IMPORT_ERROR}")
class AuthTest(unittest.TestCase):
    def setUp(self):
        # 把用户库指向临时文件，避免污染项目根目录 users.db
        fd, self.tmp_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        auth.DB_PATH = self.tmp_db
        auth._init_db()

    def tearDown(self):
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)

    def test_hash_and_verify(self):
        salt, h = auth._hash_password("secret123")
        self.assertTrue(auth._verify_password("secret123", salt, h))
        self.assertFalse(auth._verify_password("wrong", salt, h))
        # 相同密码、不同盐 → 哈希不同（随机盐生效）
        salt2, h2 = auth._hash_password("secret123")
        self.assertNotEqual(h, h2)

    def test_register_and_authenticate(self):
        auth.register_user("alice", "pass123456")
        self.assertEqual(auth.authenticate("alice", "pass123456"), "alice")
        self.assertIsNone(auth.authenticate("alice", "badpass"))
        self.assertIsNone(auth.authenticate("nobody", "pass123456"))

    def test_duplicate_register_rejected(self):
        auth.register_user("bob", "pass123456")
        with self.assertRaises(Exception):  # HTTPException 409
            auth.register_user("bob", "other123456")

    def test_short_password_rejected(self):
        with self.assertRaises(Exception):  # HTTPException 400
            auth.register_user("carol", "123")

    def test_token_roundtrip(self):
        tok = auth.create_token("dave")
        self.assertEqual(auth.decode_token(tok), "dave")
        # 篡改 token 应校验失败
        bad = tok[:-2] + ("aa" if tok[-2:] != "aa" else "bb")
        self.assertIsNone(auth.decode_token(bad))
        self.assertIsNone(auth.decode_token("not-a-jwt"))


if __name__ == "__main__":
    unittest.main()
