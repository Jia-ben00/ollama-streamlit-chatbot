"""API 层测试：用 FastAPI TestClient 打接口，mock 掉 DB 和 Ollama。

这些测试不连真实 MySQL / Ollama / Redis，全部用依赖覆盖（dependency_overrides）
和 mock 隔离。目的是验证「HTTP 层的路由、参数校验、响应格式」是否正确，
而不是验证业务正确性（那部分已经在 test_chatbot.py 里用 mock 覆盖了）。

关键技巧：
- `app.dependency_overrides[get_db]` 替换掉真实的 DB 依赖，注入一个假的 Session；
- Ollama 客户端、Redis 缓存这些外部依赖，用 unittest.mock.patch 隔离。
"""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from api.main import app
from api.deps import get_db
from db.models import Conversation, Message


class _FakeQuery:
    """极简 Query 替身：链式方法原样返回自己，终结方法返回预设结果。

    这样就能在不连数据库的前提下，验证「路由把查询拼对了没、返回格式对不对」，
    而不用为了跑一个 HTTP 层测试去起一个 MySQL。
    """

    def __init__(self, first=None, all_rows=None):
        self._first = first
        self._all = list(all_rows) if all_rows is not None else []
        self.filters = []

    # 链式方法：记录 filter 参数，便于断言（比如「有没有按 conversation_id 过滤」）
    def filter(self, *args):
        self.filters.append(args)
        return self

    def order_by(self, *args):
        return self

    def limit(self, *args):
        return self

    def group_by(self, *args):
        return self

    def outerjoin(self, *args):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all


class _FakeSession:
    """按调用顺序依次吐出预设的 Query 对象。"""

    def __init__(self, queries):
        self._queries = list(queries)
        self.added = []
        self.committed = 0

    def query(self, *entities):
        return self._queries.pop(0)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed += 1

    def refresh(self, obj):
        return None

    def execute(self, *args, **kwargs):
        return None


def _msg(mid: int, content: str, role: str = "user") -> Message:
    """构造一条未落库的 Message（只为响应序列化用）。"""
    return Message(
        id=mid,
        conversation_id=1,
        role=role,
        content=content,
        token_count=0,
        latency_ms=None,
        created_at=datetime(2026, 9, 19, 10, 0, 0),
    )


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

    @patch("api.routers.health.cache")
    @patch("api.routers.health._ollama")
    def test_ready_200_when_all_dependencies_ok(self, mock_ollama, mock_cache):
        """就绪探针：三个依赖全通 → 200，编排系统可以往这个实例打流量。"""
        app.dependency_overrides[get_db] = lambda: MagicMock()
        mock_cache.enabled = True
        mock_ollama.check_health.return_value = True

        resp = self.client.get("/health/ready")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ready")
        app.dependency_overrides.clear()

    @patch("api.routers.health.cache")
    @patch("api.routers.health._ollama")
    def test_ready_503_when_dependency_down(self, mock_ollama, mock_cache):
        """依赖缺一个就 503 —— 这正是 /health 与 /health/ready 的分工：

        /health 永远 200（liveness，管"要不要重启进程"），
        /health/ready 缺依赖就 503（readiness，管"要不要摘掉流量"）。
        """
        app.dependency_overrides[get_db] = lambda: MagicMock()
        mock_cache.enabled = False  # 模拟 Redis 挂了
        mock_ollama.check_health.return_value = True

        resp = self.client.get("/health/ready")
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["status"], "not_ready")
        self.assertFalse(body["checks"]["redis"])
        self.assertTrue(body["checks"]["database"])
        app.dependency_overrides.clear()


class TestConversations(unittest.TestCase):
    """会话 CRUD 端点（mock DB）。"""

    def setUp(self):
        self.client = TestClient(app)

    def test_create_conversation_missing_field(self):
        # 缺必填字段，Pydantic 校验应返回 422。
        resp = self.client.post("/conversations", json={"title": "x"})
        self.assertEqual(resp.status_code, 422)


class TestConversationMessages(unittest.TestCase):
    """GET /conversations/{id}/messages（游标分页拉消息）。"""

    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_returns_oldest_first(self):
        """存储层按 id 倒序取（快），返回前翻成正序（前端好渲染）。"""
        # 第 1 个 Query：会话存在性检查 → 命中
        # 第 2 个 Query：取消息 → 倒序吐出 3 条
        fake = _FakeSession(
            [
                _FakeQuery(first=(1,)),
                _FakeQuery(all_rows=[_msg(9, "第三句"), _msg(8, "第二句"), _msg(7, "第一句")]),
            ]
        )
        app.dependency_overrides[get_db] = lambda: fake

        resp = self.client.get("/conversations/1/messages")
        self.assertEqual(resp.status_code, 200)
        contents = [m["content"] for m in resp.json()]
        self.assertEqual(contents, ["第一句", "第二句", "第三句"])

    def test_cursor_sets_before_id_filter(self):
        """带 before_id 时，必须真的把它加进 WHERE（否则翻页会重复返回）。"""
        q_exists = _FakeQuery(first=(1,))
        q_msgs = _FakeQuery(all_rows=[_msg(3, "c")])
        app.dependency_overrides[get_db] = lambda: _FakeSession([q_exists, q_msgs])

        resp = self.client.get("/conversations/1/messages?before_id=5")
        self.assertEqual(resp.status_code, 200)
        # 两个 Query 都被调用过 filter：一个过滤 conversation_id，一个额外过滤 id < 5
        self.assertEqual(len(q_msgs.filters), 2)

    def test_unknown_conversation_returns_404(self):
        """会话不存在时返回 404，而不是空数组（空数组会被前端误解为「没有消息」）。"""
        app.dependency_overrides[get_db] = lambda: _FakeSession([_FakeQuery(first=None)])

        resp = self.client.get("/conversations/999/messages")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "会话不存在")

    def test_limit_validation(self):
        """limit 超过上界由 Pydantic/Query 校验拦下，返回 422。"""
        app.dependency_overrides[get_db] = lambda: _FakeSession([])
        resp = self.client.get("/conversations/1/messages?limit=9999")
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
