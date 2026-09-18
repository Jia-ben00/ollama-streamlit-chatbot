# 🤖 AI 工具箱：智能聊天 + 情感分析

[![CI](https://github.com/Jia-ben00/ollama-streamlit-chatbot/actions/workflows/ci.yml/badge.svg)](https://github.com/Jia-ben00/ollama-streamlit-chatbot/actions/workflows/ci.yml)

基于 **Python + Ollama + Streamlit + PyTorch** 构建的本地 AI 应用，包含两大功能模块：

1. **💬 智能聊天** — 基于 Ollama 本地大模型的网页版对话系统
2. **😊 情感分析** — 基于 PyTorch BiLSTM 的文本情感分类模型

所有 AI 推理均在本地完成，数据不上传云端，保护隐私。

---

## ✨ 功能特性

### 智能聊天模块
- 💬 **流式对话** — 逐字输出 AI 回复，实时交互体验
- 🔄 **多模型切换** — 自动发现本地 Ollama 已安装的模型，一键切换
- ⚙️ **参数调节** — 可视化调节 Temperature、Top P、Max Tokens
- 🎭 **自定义角色** — 通过系统提示词定义 AI 的行为风格
- 📊 **对话管理** — 清空对话、导出 JSON 格式聊天记录
- 📈 **用量统计** — 实时显示消息数和预估 Token 消耗
- 🔌 **健康检测** — 侧边栏一键检测 Ollama 服务连接状态

### 情感分析模块
- 🧠 **BiLSTM 模型** — 双向 LSTM 神经网络，PyTorch 实现
- 📊 **概率可视化** — 输出正面/负面概率分布和置信度
- 📦 **批量分析** — 支持多行文本批量情感分析
- 🎯 **高准确率** — 测试集准确率 97.0%，F1 分数 97.0%
- 📝 **示例文本** — 内置多条示例，一键加载测试

---

## 🏗️ 项目结构

```
ollama-streamlit-chatbot/
├── app.py                          # Streamlit 主应用（双模式切换）
├── requirements.txt                # 运行依赖（聊天模块，不含 torch）
├── requirements-ml.txt             # 情感分析依赖（含 torch / numpy）
├── start.bat                       # Windows 一键启动脚本（自动探测解释器）
├── .github/workflows/ci.yml        # CI：单测 + 语法检查
├── .env.example                    # 环境变量示例
├── .gitignore
├── README.md
├── src/                            # 聊天机器人模块
│   ├── __init__.py
│   ├── config.py                   # 配置管理
│   ├── ollama_client.py            # Ollama REST API 客户端
│   ├── chatbot.py                  # 聊天机器人核心逻辑
│   └── utils.py                    # 工具函数
├── sentiment_analysis/             # 情感分析模块
│   ├── __init__.py
│   ├── config.py                   # 模型超参数配置
│   ├── dataset.py                  # 英文数据集加载与预处理（含内置数据）
│   ├── dataset_chinese.py          # 中文数据集加载与预处理
│   ├── model.py                    # BiLSTM 模型定义
│   ├── train.py                    # 英文模型训练脚本
│   ├── train_chinese.py            # 中文模型训练脚本
│   ├── evaluate.py                 # 评估脚本（准确率/精确率/召回率/F1/混淆矩阵）
│   ├── predict.py                  # 单条/批量推理脚本
│   └── checkpoints/                # 训练好的模型权重
│       ├── bilstm_sentiment.pt             # 英文最佳模型检查点
│       ├── bilstm_chinese_sentiment.pt     # 中文最佳模型检查点
│       ├── vocab.json                      # 英文词汇表
│       ├── vocab_chinese.json              # 中文词汇表
│       └── training_history.json           # 训练历史
├── tests/
│   ├── __init__.py
│   └── test_chatbot.py             # 聊天模块单元测试（31 个用例）
└── assets/
```

---

## 🚀 快速开始

### 前置条件

1. **Python 3.10+**
2. **Ollama**（仅聊天模块需要）— 从 [ollama.com](https://ollama.com/) 下载安装
3. **PyTorch**（仅情感分析模块需要，约 2–3GB，单独安装）

### 安装与运行

```bash
# 1. 克隆仓库
git clone https://github.com/Jia-ben00/ollama-streamlit-chatbot.git
cd ollama-streamlit-chatbot

# 2. 创建虚拟环境（推荐）
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

# 3. 安装运行依赖（不含 PyTorch，约几十 MB）
pip install -r requirements.txt

# 4. 启动应用
streamlit run app.py
```

启动后浏览器自动打开 `http://localhost:8501`，在左侧边栏切换「智能聊天」和「情感分析」模式。

> **只想跑聊天模块？** 到此为止即可。PyTorch 只在情感分析模块里被 `import`，
> 所以不装它也能正常启动和对话 —— 这也是 CI 跑得飞快（< 1 分钟）的原因。

### Windows 一键启动

```bat
start.bat
```

脚本不写死解释器路径，按 `PYTHON_EXE` 环境变量 → `py -3` → PATH 上的 `python` 顺序探测，
依赖缺失时会提示是否自动安装：

```bat
set PYTHON_EXE=C:\Python312\python.exe
start.bat
```

### 安装情感分析依赖（PyTorch）

```bash
pip install -r requirements-ml.txt
```

它会先装 `requirements.txt` 的内容，再补上 `torch` 和 `numpy`。

### 安装 GPU 版 PyTorch（可选，加速训练）

```bash
# CUDA 12.4 版本（根据你的 CUDA 版本选择）
pip install torch --index-url https://download.pytorch.org/whl/cu124
```

---

## 💬 智能聊天模块使用

### 启动 Ollama 服务

```bash
ollama serve
```

### 拉取模型

```bash
ollama pull llama3.2      # 推荐小参数模型，适合本地运行
ollama pull qwen2:0.5b    # 更小的模型
```

> 选择模型时注意参数规模，尽量选择小参数量模型（如 0.5B~3B），确保能在本机流畅运行。

### 操作步骤

1. 在侧边栏点击「🔄 检测连接」确认 Ollama 状态
2. 从下拉框选择已安装的模型
3. 调节生成参数（Temperature / Top P / Max Tokens）
4. 可选：自定义系统提示词设定 AI 角色
5. 在底部输入框开始对话

---

## 😊 情感分析模块使用

### 模型架构

```
输入文本 → 分词 → 词嵌入(Embedding) → 双向LSTM(2层) → Dropout → 全连接层 → Softmax → 正面/负面概率
```

- **参数量**：约 73.8 万
- **词汇表**：608 词
- **最大序列长度**：200 token

### 训练模型

```bash
# 使用内置数据集训练（1166 条英文影评，无需额外下载）
python -m sentiment_analysis.train
```

训练过程会自动：
1. 加载并打乱内置数据集（正面 627 条 / 负面 539 条）
2. 按 8:2 划分训练集/测试集
3. 构建词汇表
4. 训练 10 个 epoch，自动保存最佳模型

> 也可使用外部 CSV 数据集：在 `create_dataloaders(csv_path="your_data.csv")` 中指定路径，CSV 需包含 `text` 和 `label` 列（0=负面，1=正面）。

### 评估模型

```bash
python -m sentiment_analysis.evaluate
```

输出准确率、精确率、召回率、F1 分数和混淆矩阵。

### 命令行推理

```bash
python -m sentiment_analysis.predict
```

进入交互式推理，输入文本即可获得情感预测结果。

### 训练结果

| 指标 | 值 |
|------|-----|
| 测试集准确率 | 97.01% |
| 宏平均精确率 | 97.02% |
| 宏平均召回率 | 96.96% |
| 宏平均 F1 | 96.99% |
| 最佳 Epoch | 6/10 |

**混淆矩阵**（234 条测试数据）：

| | 预测负面 | 预测正面 |
|---|---|---|
| 真实负面 | 104 | 4 |
| 真实正面 | 3 | 123 |

---

## 🧪 运行测试

```bash
# 聊天模块单元测试（31 个用例，mock 离线运行，不需要 Ollama）
python -m unittest discover tests -v
```

### CI

`.github/workflows/ci.yml` 在每次 push / PR 时跑：语法检查 → 单元测试，Python 3.11，期望 **31 passed**。

CI 里刻意**只装 `requirements.txt`**（不含 torch），并有一条 guard 步骤会在 torch 意外出现时直接失败：
装了 torch 的话每次 run 要多下 2–3GB，这正是「本地跑通 ≠ CI 跑通」最常见的坑。

---

## 🔧 技术栈

| 模块 | 技术 |
|------|------|
| Web 框架 | Streamlit |
| 大模型推理 | Ollama（本地 LLM 服务） |
| 深度学习框架 | PyTorch |
| 情感模型 | BiLSTM（双向长短期记忆网络） |
| HTTP 客户端 | requests |
| 配置管理 | python-dotenv + dataclasses |
| 测试 | unittest + mock |
| CI | GitHub Actions（ubuntu-latest / Python 3.11） |

---

## 📄 License

MIT License
