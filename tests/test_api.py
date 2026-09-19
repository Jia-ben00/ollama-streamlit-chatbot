"""API 层测试：用 FastAPI TestClient 打接口，mock 掉 DB 和 Ollama。

这些测试不连真实 MySQL / Ollama / Redis，全部用依赖覆盖（dependency_overrides）
和 mock 隔离。目的是验证「HTTP 层的路由、参数校验、响应格式」是否正确，
而不是验证业务正确性（那部分已经在 test_chatbot.py 里用 mock 覆盖了）。

关键技巧：
- `app.dependency_overrides[get_db]` 替换掉真实的 DB 依赖，注入一个假的 Session；
- Ollama 客户端、Redis 缓存这些外部依赖，用 unittest.mock.patch 隔离。
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from api.main import app
from api.deps import get_db
from db.models import Conversation, Message


class TestHealth(unittest.TestCase):
    """健康检查端点。"""

    def setUp(self):
        self.client = TestClient(app)

    @patch("api.routers.health.cache")
    @patch("api.routers.health._ollama")
    def test_health_all_ok(self, mock_ollama, mock_cache):
        # mock 掉 DB 依赖：假 Session 执行 SELECT 1 不报错即视为健康。
        mock_db = MagicMock()
        app.dependency_overrides[get_db] = lambda: mock_db

        # cache.enabled = True，_ollama.check_health() = True。
        mock_cache.enabled = True
        mock_ollama.check_health.return_value = True

        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["checks"]["database"])
        self.assertTrue(body["checks"]["redis"])
        self.assertTrue(body["checks"]["ollama"])

        app.dependency_overrides.clear()

    @patch("api.routers.health.cache")
    @patch("api.routers.health._ollama")
    def test_health_ollama_down(self, mock_ollama, mock_cache):
        mock_db = MagicMock()
        app.dependency_overrides[get_db] = lambda: mock_db
        mock_cache.enabled = True
        mock_ollama.check_health.return_value = False

        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)  # 业务上仍是 200，但 status=degraded
        body = resp.json()
        self.assertEqual(body["status"], "degraded")
        self.assertFalse(body["checks"]["ollama"])

        app.dependency_overrides.clear()

    def test_root(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("service", resp.json())


class TestConversations(unittest.TestCase):
    """会话 CRUD 端点（mock DB）。"""

    def setUp(self):
        self.client = TestClient(app)

    def test_create_conversation_missing_field(self):
        # 缺必填字段，Pydantic 校验应返回 422。
        resp = self.client.post("/conversations", json={"title": "x"})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
