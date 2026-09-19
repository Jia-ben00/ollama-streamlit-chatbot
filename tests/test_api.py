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
from db.models import Conversation, Message, Model, User


class _FakeQuery:
    """极简 Query 替身：链式方法原样返回自己，终结方法返回预设结果。

    这样就能在不连数据库的前提下，验证「路由把查询拼对了没、返回格式对不对」，
    而不用为了跑一个 HTTP 层测试去起一个 MySQL。
    """

    def __init__(self, first=None, all_rows=None, deleted=3, scalar=3):
        self._first = first
        self._all = list(all_rows) if all_rows is not None else []
        self._deleted = deleted
        self._scalar = scalar
        self.filters = []

    # 链式方法：记录 filter 参数，便于断言（比如「有没有按 conversation_id 过滤」）
    def filter(self, *args, **kwargs):
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

    def delete(self, **kwargs):
        """DELETE 语句的终结方法，返回受影响行数。"""
        return self._deleted

    def scalar(self):
        return self._scalar


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


def _conv(is_archived: bool = False, model_id: int = 1, title: str = "测试会话") -> Conversation:
    """构造一个未落库的 Conversation（只为响应序列化用）。"""
    return Conversation(
        id=1,
        user_id=1,
        model_id=model_id,
        title=title,
        is_archived=is_archived,
        created_at=datetime(2026, 9, 19, 10, 0, 0),
        updated_at=datetime(2026, 9, 19, 10, 0, 0),
    )


class TestCatalog(unittest.TestCase):
    """GET /models 与 GET /users。

    这两个接口是「前端接后端」时才补上的：前端要建会话就必须知道 user_id /
    model_id，而这两个 id 只有服务端知道。测试它们，等于测试「前端能不能启动」。
    """

    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_models_returns_selectable_fields(self):
        """模型列表要带齐前端选择器需要的字段（id 用来提交，name 用来展示）。"""
        fake = _FakeSession(
            [
                _FakeQuery(
                    all_rows=[
                        Model(
                            id=1,
                            name="llama3.2",
                            provider="ollama",
                            param_size="3B",
                            context_window=8192,
                            is_active=True,
                        )
                    ]
                )
            ]
        )
        app.dependency_overrides[get_db] = lambda: fake

        resp = self.client.get("/models")
        self.assertEqual(resp.status_code, 200)
        item = resp.json()[0]
        self.assertEqual(item["id"], 1)
        self.assertEqual(item["name"], "llama3.2")
        self.assertTrue(item["is_active"])

    def test_users_does_not_leak_email(self):
        """响应模型是「白名单」：表里有 email，也不代表接口该把它发出去。

        邮箱是 PII。一个「选当前用户」的下拉框只需要 username。
        这条测试锁住的正是「不要直接把 ORM 对象 return 出去」这个原则 ——
        一旦有人图省事改成返回 ORM 对象，email 会立刻泄漏，这条测试就会红。
        """
        fake = _FakeSession(
            [
                _FakeQuery(
                    all_rows=[
                        User(
                            id=1,
                            username="alice",
                            email="alice@example.com",
                            plan="free",
                        )
                    ]
                )
            ]
        )
        app.dependency_overrides[get_db] = lambda: fake

        resp = self.client.get("/users")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()[0]
        self.assertNotIn("email", body)
        self.assertEqual(body["username"], "alice")


class TestConversationUpdate(unittest.TestCase):
    """PATCH /conversations/{id}：局部更新（改标题 / 换模型 / 归档）。"""

    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_no_body_still_archives(self):
        """回归：不传 body 仍然归档。

        这是第一版接口的行为，tests/e2e/smoke.py 依赖它。第二版加了「换模型」
        之后，很容易顺手把它改成「必须传 body」，从而悄无声息地打断老调用方 ——
        这条测试就是为了防止那次「顺手」。
        """
        app.dependency_overrides[get_db] = lambda: _FakeSession([_FakeQuery(first=_conv()), _FakeQuery()])
        resp = self.client.patch("/conversations/1")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_archived"])

    def test_explicit_unarchive(self):
        """显式传 is_archived=false 要能取消归档（旧接口做不到这件事）。"""
        app.dependency_overrides[get_db] = lambda: _FakeSession(
            [_FakeQuery(first=_conv(is_archived=True)), _FakeQuery()]
        )
        resp = self.client.patch("/conversations/1", json={"is_archived": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["is_archived"])

    def test_switch_model(self):
        """换模型：前端切换模型时唯一可走的路（模型绑定在会话上，不是请求参数）。"""
        fake = _FakeSession([_FakeQuery(first=_conv(model_id=1)), _FakeQuery(first=(2,)), _FakeQuery()])
        app.dependency_overrides[get_db] = lambda: fake

        resp = self.client.patch("/conversations/1", json={"model_id": 2})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["model_id"], 2)

    def test_invalid_model_id_returns_400_not_500(self):
        """非法 model_id 必须是 400，不能让外键约束在 commit 时抛成 500。

        「外键约束」保证的是数据一致性，不是错误语义。边界层不校验的话，
        调用方拿到的是 500（以为服务挂了），而不是 400（知道是自己传错了）。
        """
        fake = _FakeSession([_FakeQuery(first=_conv()), _FakeQuery(first=None)])
        app.dependency_overrides[get_db] = lambda: fake

        resp = self.client.patch("/conversations/1", json={"model_id": 999})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "模型不存在")

    def test_unsent_fields_untouched(self):
        """PATCH 的「局部」含义：没传的字段一个都不能动。"""
        app.dependency_overrides[get_db] = lambda: _FakeSession(
            [_FakeQuery(first=_conv(title="原标题")), _FakeQuery(first=(1,)), _FakeQuery()]
        )
        resp = self.client.patch("/conversations/1", json={"model_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "原标题")


class TestClearMessages(unittest.TestCase):
    """DELETE /conversations/{id}/messages：清空消息但保留会话。"""

    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch("api.routers.conversations.cache")
    def test_clears_and_invalidates_cache(self, mock_cache):
        """清空消息后**必须**让上下文缓存失效。

        只删库不清缓存的话，下一次提问会把「已经被用户删掉的消息」重新拼进
        prompt 喂给模型 —— 接口全返回成功、数据库里也确实没有那些行，
        排查起来极其痛苦。所以这里同时断言两件事：删了多少行、缓存有没有被失效。
        """
        app.dependency_overrides[get_db] = lambda: _FakeSession(
            [_FakeQuery(first=(1,)), _FakeQuery(deleted=7)]
        )

        resp = self.client.delete("/conversations/1/messages")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted"], 7)
        mock_cache.invalidate.assert_called_once_with(1)

    @patch("api.routers.conversations.cache")
    def test_unknown_conversation_returns_404(self, mock_cache):
        """会话不存在时 404，且不能白删一轮、也不该去失效别人的缓存。"""
        app.dependency_overrides[get_db] = lambda: _FakeSession([_FakeQuery(first=None)])

        resp = self.client.delete("/conversations/999/messages")
        self.assertEqual(resp.status_code, 404)
        mock_cache.invalidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
