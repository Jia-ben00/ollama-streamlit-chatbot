"""Streamlit 界面测试：用官方无头测试框架 AppTest 真跑一遍 app.py。

**为什么这件事值得做。** 长期以来 Streamlit 应用被当成「测不了的东西」——
它依赖页面上下文、事件循环、浏览器会话，一个 `st.button()` 的返回值
取决于「用户点没点」。结果就是：后端测得很细，界面全靠手点。

但 streamlit ≥ 1.28 提供了 `streamlit.testing.v1.AppTest`：它在一个无头环境里
执行整个脚本、把控件树建出来、允许程序化地设值和重跑，并捕获异常。
于是「界面改动把页面搞崩了」这类问题，终于能在 CI 里被拦住。
**这也正是把聊天逻辑抽进 ChatSession 之后的回报**：界面里剩下的只有
「控件如何编排」，变得可以端到端地跑。

**关于用例数量：这里刻意合并同类断言，而不是「一个测试只断言一件事」。**
因为每一次 `AppTest.run()` 都要把 app.py 完整执行一遍，实测约 6 秒；
9 个用例跑下来接近 60 秒，比整个后端测试套件还慢 200 倍。
而 UI 断言的粒度本来就不一样——「页面起得来吗」是一个整体判断，
拆成四条去跑四遍同样的页面，付出的是时间、买到的是虚假的独立性。
所以：**按「需要几次页面执行」来组织用例**，而不是按断言条数。
（这个取舍本身值得讲：测试也是要有成本意识的。）

这套测试确实抓到过真问题：切到「后端 API」数据源而后端没启动时，
标题栏调 `current_model()` 抛异常，整页变红色报错页。
修完之后，那条回归用例已经降级放进 `tests/test_chat_session.py`（毫秒级）。
"""

import unittest
from pathlib import Path

# 没有 streamlit 的环境（比如只想跑后端测试的机器）不该因为导入失败而整个
# 测试套件跑不起来。但**跳过不等于通过**：CI 里有一条 guard 步骤会确认
# streamlit 确实装着，避免这些用例在某天悄悄全被跳过还没人发现。
try:
    from streamlit.testing.v1 import AppTest

    _APPTEST_AVAILABLE = True
except ImportError:  # pragma: no cover
    AppTest = None
    _APPTEST_AVAILABLE = False

requires_apptest = unittest.skipIf(
    not _APPTEST_AVAILABLE, "未安装 streamlit，跳过界面测试"
)

# 用绝对路径定位 app.py：不管从哪个工作目录运行测试（CI 从仓库根、
# 本地可能从别处），都能找到同一个文件。
APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

# 首次 import streamlit 比较慢，且脚本里会尝试连 Ollama（连不上会等 socket），
# 默认 3 秒不够用。
TIMEOUT = 60


@requires_apptest
class TestAppBoots(unittest.TestCase):
    """首屏：能跑起来 + 关键控件在 + 默认落在本地直连。"""

    def test_first_render(self):
        at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT).run()

        self.assertEqual(
            list(at.exception), [], f"app.py 首屏抛异常：{list(at.exception)}"
        )

        # 默认必须是直连模式。理由不是审美：直连零依赖（不需要 MySQL、
        # 不需要先启动后端），是「clone 下来就能看」的最短路径。
        # 默认切到后端模式，会让第一次运行的人迎面撞上一个红条。
        self.assertEqual(at.radio(key="data_source").value, "local")

        # 聊天模块的核心交互控件。
        self.assertEqual(len(at.chat_input), 1)

        # 无论 Ollama 通不通，都要有状态反馈，不能是一片空白。
        # 这条断言刻意宽松（不断言成功还是失败）：测试机可能装了 Ollama，
        # CI 上一定没有——**测试不该依赖机器上装没装什么东西**。
        feedback = list(at.success) + list(at.error) + list(at.info)
        self.assertGreater(len(feedback), 0, "服务状态区没有任何反馈")


@requires_apptest
class TestSourceSwitch(unittest.TestCase):
    """数据源切换：本轮新增的界面能力，也是最容易出问题的地方。"""

    def test_api_mode_degrades_gracefully_and_explains_how_to_start(self):
        """切到后端模式（且后端没启动）时，页面要优雅降级并给出启动命令。

        这是最真实的场景：用户看到「后端 API（MySQL）」这个选项，点一下，
        而服务没起。这时该看到一句「请先启动后端」加一条可复制的命令，
        而不是 Streamlit 的异常页。
        """
        at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT).run()
        at.radio(key="data_source").set_value("api").run()

        self.assertEqual(
            list(at.exception), [], f"切到 api 模式抛异常：{list(at.exception)}"
        )

        texts = [e.value for e in at.error] + [i.value for i in at.info]
        self.assertIn("uvicorn", " ".join(texts))

        # 后端不通时不画模型下拉——没有数据源的控件不该假装能用。
        # 「看起来能用但点了没反应」比「明说不能用」糟糕得多。
        self.assertNotIn("model_picker_api", [sb.key for sb in at.selectbox])

    def test_switching_back_and_forth_keeps_working(self):
        """来回切换不能把状态搞坏（切回来时不能残留另一边的状态）。"""
        at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT).run()
        at.radio(key="data_source").set_value("api").run()
        at.radio(key="data_source").set_value("local").run()

        self.assertEqual(list(at.exception), [])
        self.assertEqual(at.radio(key="data_source").value, "local")


@requires_apptest
class TestSentimentModule(unittest.TestCase):
    """另一个功能模块不受影响（回归：改聊天模块别把隔壁搞坏）。"""

    def test_module_switch_does_not_crash(self):
        """切到情感分析模块时，缺 PyTorch 也只能报错、不能崩。

        这条跑在**没装 torch** 的机器上——正是 CI 的环境。它的价值在于锁住
        「重依赖必须在函数内部 import」这个设计：如果哪天有人把 `import torch`
        挪到模块顶层，这个测试会立刻变红，因为连界面都加载不出来了。
        """
        at = AppTest.from_file(APP_PATH, default_timeout=TIMEOUT).run()
        # 第一个 radio 是功能导航（在脚本里先渲染），第二个才是数据源。
        at.sidebar.radio[0].set_value("😊 情感分析").run()

        self.assertEqual(
            list(at.exception), [], f"情感分析模块抛异常：{list(at.exception)}"
        )


if __name__ == "__main__":
    unittest.main()
