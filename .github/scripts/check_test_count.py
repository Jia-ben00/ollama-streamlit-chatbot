#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断言 README 里声明的用例数 == CI 实际跑出来的用例数。

CI 里的用法（先把测试输出 tee 成日志，再喂给本脚本）：

    python -m unittest discover tests -v 2>&1 | tee "$RUNNER_TEMP/unittest.log"
    python .github/scripts/check_test_count.py "$RUNNER_TEMP/unittest.log"

为什么需要这一条
----------------
README 承诺「全部测试（261 个用例，…）」与「期望 **261 passed**」，但 CI 只跑
unittest：测试**失败**会红，**数量变了却不会红**。删掉一个测试文件，CI 照样全绿，
README 里那个数字就悄悄变成了假话。这条断言把「说过的话」变成机器可验证的约束。

为什么比对「CI 日志」而不是自己再 collect 一遍
----------------------------------------------
用例数取决于运行环境。本机裸环境（没装 requirements）collect 到 64 个，其中 11 个
模块因为缺 fastapi / sqlalchemy / yaml 而 import 失败；CI 装完 requirements 是 261 个。
自己 collect 会得到一个和 CI 不一样的数字，断言就成了摆设。
直接读 CI 那次**真实运行**打印的 `Ran N tests`，口径与 CI 完全一致，也不需要把测试
再跑一遍。

fail-closed：日志里找不到 `Ran N tests`、README 读不到声明、两处声明互相矛盾 —— 一律失败。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
README = os.path.join(ROOT, "README.md")


def fail(msg):
    print(f"::error:: {msg}")
    raise SystemExit(1)


def main():
    # ---- 1) CI 实际跑出来的用例数 ------------------------------------------
    if len(sys.argv) < 2:
        fail("用法: check_test_count.py <unittest 输出日志>。"
             "CI 里先 `... 2>&1 | tee <log>` 把测试输出存下来，再把日志路径传进来。")
    log_path = sys.argv[1]
    if not os.path.isfile(log_path):
        fail(f"找不到 unittest 输出日志: {log_path}（测试步骤是否真的把输出 tee 到这里了？）")
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        log = fh.read()

    hits = re.findall(r"^Ran (\d+) tests? in .*$", log, re.M)
    if not hits:
        fail("日志里没有 `Ran N tests in …` 这一行 —— 测试没有真正跑完，按失败处理")
    if len(set(hits)) != 1:
        fail(f"日志里出现了多个互相矛盾的 `Ran N tests`: {sorted(set(hits))}")
    actual = int(hits[0])

    # ---- 2) README 里的声明 ------------------------------------------------
    if not os.path.isfile(README):
        fail(f"README.md 不存在: {README}")
    with open(README, encoding="utf-8") as fh:
        text = fh.read()

    # 注意：「N 个用例」在 README 里不止一处 —— 项目结构注释里还写着
    #   `test_chatbot.py  # 聊天模块单元测试（31 个用例）`
    # 那是**单个文件**的用例数，不是总量。所以这里必须锚定「全部测试」这个词，
    # 否则会把 31 当成总量，断言永远失败。（这是本脚本第一版踩到的坑。）
    pat_cn = re.compile(r"全部测试\s*[（(]\s*(\d+)\s*个用例")
    pat_en = re.compile(r"期望\s*\**\s*(\d+)\s*passed", re.I)
    found_cn = [int(m.group(1)) for m in pat_cn.finditer(text)]
    found_en = [int(m.group(1)) for m in pat_en.finditer(text)]

    if not found_cn:
        fail("README 里找不到「全部测试（N 个用例…）」的声明 —— 无法校验，按失败处理")
    if not found_en:
        fail("README 里找不到「期望 N passed」的声明 —— 无法校验，按失败处理")

    declared = set(found_cn) | set(found_en)
    if len(declared) != 1:
        fail(f"README 内两处声明互相矛盾：「N 个用例」={found_cn}，「期望 N passed」={found_en}")
    declared_n = declared.pop()

    print(f"README 声明 = {declared_n} | CI 实际 = {actual}")

    # ---- 3) 比对 -----------------------------------------------------------
    if declared_n != actual:
        verb = "少" if actual < declared_n else "多"
        fail(f"README 声明 {declared_n} 个用例，CI 实际跑了 {actual} 个"
             f"（{verb} {abs(actual - declared_n)} 个）。请同步 README 里的两处数字。")
    print(f"用例数一致: {actual} ✓")


if __name__ == "__main__":
    main()
