"""`src/stream_probe.py` 的守卫：证明这把尺子「量得出东西」，而不是恒返回「正常」。

背景：公网入口唯一能在客户端侧验证的东西就是「流式有没有退化成一次性返回」。
如果这把尺子写松了（比如把「块数够」当成「流式正常」），它会安静地把
`proxy_buffering on` 的部署判成通过——而那种失败上线后几乎没人会发现，
因为功能上是好的，只是「一个字一个字蹦出来」变成了「转圈等到最后出全文」。

所以这里除了正常用例，还有两条**能力检查**：
- 攒批样本必须被判成 `BUFFERED`（尺子有报错的能力）
- 尺子不能是恒函数、阈值必须真的参与判定（不是摆设的参数）

真实链路里的对照见 `tests/e2e/public_check.py` 与 `tests/e2e/buffering_proxy.py`。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stream_probe import (  # noqa: E402
    BUFFERED,
    INCONCLUSIVE,
    INCREMENTAL,
    judge_incremental,
)


class TestVerdicts(unittest.TestCase):
    def test_chunks_arriving_step_by_step_is_incremental(self):
        """假 Ollama 的节奏：9 块、每块间隔 50ms。"""
        arrivals = [round(0.05 * i, 3) for i in range(9)]  # 0.00 .. 0.40
        verdict, m, reason = judge_incremental(arrivals, total=0.45)
        self.assertEqual(verdict, INCREMENTAL, reason)
        self.assertEqual(m["chunks"], 9)
        self.assertAlmostEqual(m["span"], 0.40, places=3)

    def test_slow_real_model_pace_is_still_incremental(self):
        """真实模型的节奏慢得多（每块 400ms）。

        这条是为了钉住一件事：判据**不能**依赖「块间隔落在某个区间」——
        那样就是把本地假 Ollama 的节奏当尺子，换个真模型立刻恒假红。
        """
        arrivals = [0.1, 0.5, 0.9, 1.3, 1.7, 2.1]
        verdict, _, reason = judge_incremental(arrivals, total=2.4)
        self.assertEqual(verdict, INCREMENTAL, reason)

    def test_all_chunks_arriving_together_is_buffered(self):
        """攒批的形态：9 块照样给你 9 个事件，但全落在同一瞬间。"""
        arrivals = [0.40, 0.402, 0.405, 0.408, 0.41, 0.412, 0.415, 0.418, 0.42]
        verdict, m, reason = judge_incremental(arrivals, total=0.42)
        self.assertEqual(verdict, BUFFERED, reason)
        self.assertLess(m["span"], 0.25)

    def test_one_burst_early_in_the_stream_is_buffered(self):
        """3 块全挤在 4ms 内、但首块相对总耗时很早 —— 这条单独钉住**跨度**判据。

        为什么要专门造这个样本：现实里「一起到达」几乎总是同时伴随「首块来得太晚」
        （攒完再发，首块自然贴近末尾），于是「首块位置」那条会先命中，
        **「跨度」这条到底在不在起作用就看不出来了**。

        这不是推测：反向对照把跨度判定删掉后，其它样本照样全红，只有这条会漏
        （见 tests/e2e/reverse_check.py 的 stream_probe 组）。所以断言里
        专门写了一条「首块位置那条不成立」，把隔离关系钉死。
        """
        verdict, m, reason = judge_incremental([0.05, 0.052, 0.054], total=1.0)
        self.assertEqual(verdict, BUFFERED, reason)
        self.assertLess(m["span"], 0.25, "本样本的跨度应当触发判据")
        self.assertLess(m["first"], 0.5 * m["total"], "本样本不该由「首块位置」判据命中")

    def test_content_flushed_only_at_the_end_is_buffered(self):
        """另一种形态：块之间有间隔，但**首块来得太晚**（生成完才开始吐）。

        这条走的是「首块位置」那条判据 —— 和上面那条互相独立，
        单靠「看跨度」拦不住它（跨度 0.25s 刚好达标）。
        """
        verdict, m, reason = judge_incremental([0.5, 0.625, 0.75], total=0.8)
        self.assertEqual(verdict, BUFFERED, reason)
        self.assertGreater(m["first"], 0.5 * m["total"])

    def test_too_few_chunks_is_inconclusive_not_pass(self):
        """块数不足时**不能**判通过。

        这是最容易写错的一处：`len(chunks) >= 1` 就返回 True 的话，
        一个只回了一个字的响应会被判成「流式正常」——而它其实什么都没证明。
        """
        verdict, _, reason = judge_incremental([0.40], total=0.41)
        self.assertEqual(verdict, INCONCLUSIVE, reason)

    def test_no_chunks_at_all_is_inconclusive(self):
        verdict, m, _ = judge_incremental([], total=30.0)
        self.assertEqual(verdict, INCONCLUSIVE)
        self.assertEqual(m["chunks"], 0)


class TestRulerIsNotBlind(unittest.TestCase):
    """尺子本身也要被校验：一个量不出东西的尺子，和一个量出「一切都好」的尺子长得一样。"""

    def test_buffered_samples_are_never_reported_as_incremental(self):
        """能力检查：把各种「攒批」形态喂进去，一次都不许判成 INCREMENTAL。"""
        buffered_samples = [
            ([0.42] * 9, 0.42),                       # 完全同时
            ([0.30, 0.301, 0.302], 0.31),             # 极窄窗口
            ([0.5, 0.625, 0.75], 0.8),                # 首块过晚
            ([0.70, 0.701, 0.702, 0.703], 0.71),      # 末尾一次性
        ]
        for arrivals, total in buffered_samples:
            with self.subTest(arrivals=arrivals):
                verdict, _, reason = judge_incremental(arrivals, total=total)
                self.assertEqual(verdict, BUFFERED, reason)

    def test_verdict_is_not_constant(self):
        """尺子不是恒函数：同一批输入里必须同时出现「通过」和「不通过」。"""
        verdicts = {
            judge_incremental([0.05 * i for i in range(9)], total=0.45)[0],
            judge_incremental([0.42] * 9, total=0.42)[0],
            judge_incremental([0.40], total=0.41)[0],
        }
        self.assertEqual(verdicts, {INCREMENTAL, BUFFERED, INCONCLUSIVE})

    def test_thresholds_actually_participate(self):
        """阈值必须是真参数：把它调紧/调松，判定要跟着变。

        阈值的意义在于「判定确实读了它」。如果把它改成任意值结论都不变，
        那它就不是阈值，只是看起来像配置的常量。
        """
        arrivals = [0.05 * i for i in range(9)]  # 跨度 0.40、首块 0.00
        self.assertEqual(judge_incremental(arrivals, total=0.45)[0], INCREMENTAL)
        strict, _, _ = judge_incremental(arrivals, total=0.45, min_span=0.50)
        self.assertEqual(strict, BUFFERED, "min_span 调紧后没变红，说明它没参与判定")

        tight = [0.30, 0.301, 0.302]  # 跨度 0.002、首块 0.30：两条判据都指向「攒批」
        self.assertEqual(judge_incremental(tight, total=0.31)[0], BUFFERED)
        loose, _, reason = judge_incremental(
            tight, total=0.31, min_span=0.001, first_ratio=0.99
        )
        self.assertEqual(loose, INCREMENTAL, "阈值调松后结论没变，说明它们没生效：" + reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
