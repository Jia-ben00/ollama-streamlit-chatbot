# 🤖 AI 工具箱：智能聊天 + 情感分析

[![CI](https://github.com/Jia-ben00/ollama-streamlit-chatbot/actions/workflows/ci.yml/badge.svg)](https://github.com/Jia-ben00/ollama-streamlit-chatbot/actions/workflows/ci.yml)

基于 **Python + Ollama + FastAPI + MySQL/Redis + PyTorch** 构建的本地 AI 应用，包含两层：

1. **💬 智能聊天** — 基于 Ollama 本地大模型的网页版对话系统
2. **😊 情感分析** — 基于 PyTorch BiLSTM 的文本情感分类模型

聊天部分已经从「Streamlit 单进程」改造为 **前后端分离**：FastAPI 提供 HTTP 层，
MySQL 做消息持久化，Redis 缓存会话上下文，SSE 流式输出。所有 AI 推理仍在本地完成。

- 部署手册：[docs/DEPLOY.md](docs/DEPLOY.md)
- 面试自查（每个技术选型的理由 + 实测数据）：[docs/interview-notes.md](docs/interview-notes.md)

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
├── src/                            # 领域层 + 客户端层
│   ├── __init__.py
│   ├── config.py                   # 配置管理
│   ├── ollama_client.py            # Ollama REST API 客户端（直连模式用）
│   ├── chatbot.py                  # 聊天机器人核心逻辑
│   ├── api_client.py               # 后端 API 客户端（后端模式用，消费 SSE）
│   ├── chat_session.py             # 会话数据源抽象：直连 / 后端两种实现
│   └── utils.py                    # 工具函数
├── api/                            # HTTP 层（FastAPI，后端化新增）
│   ├── main.py                     # FastAPI 入口 + lifespan（连接池生命周期）
│   ├── deps.py                     # 依赖注入（DB session）
│   ├── schemas.py                  # Pydantic 请求/响应模型
│   └── routers/
│       ├── chat.py                 # POST /chat（SSE 流式）
│       ├── conversations.py        # 会话 CRUD + 消息列表 / 清空 + 局部更新
│       ├── catalog.py              # GET /models、GET /users（前端启动所需）
│       └── health.py               # /health（存活+诊断）与 /health/ready（就绪）
├── db/                             # 数据层（SQLAlchemy ORM，后端化新增）
│   ├── models.py                   # 6 张表的 ORM 映射（对齐本地 chatbot 库）
│   ├── session.py                  # 引擎 + 连接池 + get_db()
│   └── init_db.py                  # 建表脚本
├── cache.py                        # Redis 会话上下文缓存（连不上自动降级）
├── Dockerfile                      # 多阶段构建
├── docker-compose.yml              # api + mysql + redis（healthcheck + depends_on）
├── .dockerignore                   # 构建上下文排除（.env 不进镜像 —— 这是一条凭据边界）
├── deploy.sh                       # 云主机一键部署脚本
├── .env.prod.example               # 生产环境变量模板
├── .gitattributes                  # 换行符策略（sh 用 LF / bat 用 CRLF）
├── docs/
│   ├── DEPLOY.md                   # 部署手册（上云步骤 / 安全组 / 排查）
│   └── interview-notes.md          # 面试自查：6 个必问点 + 实测数据
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
│   ├── test_chatbot.py             # 聊天模块单元测试（31 个用例）
│   ├── test_api.py                 # API 层测试（TestClient + 假 Session）
│   ├── test_api_client.py          # 前端客户端测试（SSE 解析、错误映射）
│   ├── test_chat_session.py        # 会话抽象层测试（两种数据源的语义）
│   ├── test_app.py                 # 界面测试（Streamlit AppTest 无头跑 app.py）
│   ├── test_cache.py               # 缓存层测试（假 Redis，含降级行为）
│   ├── test_deploy_manifest.py     # 部署清单自洽性校验（compose / Dockerfile / .env）
│   ├── test_deploy_manifest_guards.py  # 元测试：证明上面那些守卫真的会失败
│   └── e2e/                        # 端到端（需外部服务，不进 CI，详见其 README）
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

## 🌐 后端 API（后端化改造）

原来的 Streamlit 应用是「进程内直接调 Ollama」，没有持久化、没有并发能力。这一层把它拆成
**HTTP 层（FastAPI）+ 数据层（MySQL/SQLAlchemy）+ 缓存层（Redis）**，前端可以换、可以并存。

### 本地起服务

```bash
# 1. 建库（MySQL 8.0 需要先跑着）
mysql -uroot -p -e "CREATE DATABASE chatbot CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"

# 2. 配置连接串（.env，已在 .gitignore 里，不会进仓库）
#    DATABASE_URL=mysql+pymysql://root:<密码>@127.0.0.1:3306/chatbot?charset=utf8mb4

# 3. 建表（读 db/models.py 的 ORM，幂等可重跑）
python -m db.init_db

# 4. 起服务
uvicorn api.main:app --reload --port 8000
```

打开 `http://127.0.0.1:8000/docs` 可以直接在浏览器里调接口。

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活 + 诊断：永远 200，body 里逐项报告 DB / Redis / Ollama |
| GET | `/health/ready` | 就绪：依赖全通才 200，否则 503（给编排系统摘流量用） |
| GET | `/models` | 可选模型列表（前端建会话需要 `model_id`） |
| GET | `/users` | 用户列表（演示用，响应里不含 email） |
| POST | `/conversations` | 创建会话 |
| GET | `/conversations?user_id=` | 会话列表（含消息数，单条 SQL 防 N+1） |
| GET | `/conversations/{id}` | 单个会话 |
| GET | `/conversations/{id}/messages` | 消息列表（游标分页 `before_id`，任意页深成本恒定） |
| DELETE | `/conversations/{id}/messages` | 清空消息（保留会话；会连带让上下文缓存失效） |
| PATCH | `/conversations/{id}` | 局部更新：改标题 / **换模型** / 归档 |
| POST | `/chat` | **SSE 流式对话**，流结束后落 assistant 消息并记 `latency_ms` |

### 前端怎么接上后端

原来的 Streamlit 界面是「进程内直连 Ollama」，它**完全不知道上面这些接口的存在**。
把它接上去时做了两件事：

1. `src/api_client.py` — HTTP 客户端，消费 `/chat` 的 SSE 流；
2. `src/chat_session.py` — 把「一个会话」抽象成接口，给出两个实现：

   | | `LocalChatSession` | `APIChatSession` |
   |---|---|---|
   | 消息存在哪 | 进程内存 | MySQL |
   | 关掉页面 | 对话没了 | 还在 |
   | 系统提示词 / 生成参数 | 可调 | 服务端统一提供（前端置灰并说明原因） |
   | 换模型 | 改内存状态 | 改会话绑定的 `model_id`（写库） |

于是界面代码**只写一遍**，用侧边栏的「数据源」下拉框切换。这就是「前后端分离」
在代码里的具体样子——不是「有个 API 就算分离了」，而是**换后端不用改界面**。

跑起来：

```bash
# 终端 1
uvicorn api.main:app --port 8000
# 终端 2（同项目目录）
streamlit run app.py
```

在侧边栏把「数据源」切到 **🗄️ 后端 API（MySQL）** 即可：新建会话、切模型、
清空对话都会真正落到数据库里；后端没启动时界面会给出启动命令，而不是报错页。

### 容器化部署

```bash
cp .env.prod.example .env && vi .env    # 填 MYSQL_ROOT_PASSWORD
bash deploy.sh                          # 构建 + 起服 + 轮询就绪 + 打印验证命令
```

详细步骤（安全组、Ollama 接法、Nginx 反代、故障排查、回滚）见 **[docs/DEPLOY.md](docs/DEPLOY.md)**。

> 只暴露 API 端口：`docker-compose.yml` 故意不把 3306 / 6379 映射到宿主机 ——
> 把无密码的 Redis 或数据库端口放到公网，是历史上大量服务器被入侵的直接原因。

**上云之前跑一遍清单校验**（就是 `python -m unittest tests.test_deploy_manifest`，
每次 CI 也会跑）。这类问题在本机是看不见的：本机没装 Docker，compose 写错了跑不到；
就算装了 Docker Desktop（Mac/Windows），`host.docker.internal` 默认能解析，而云主机是 Linux。
实测拦下来的几类：

| 缺陷 | 后果 | 本机为什么发现不了 |
|---|---|---|
| 缺 `.dockerignore`，`COPY . .` 把 `.env` 一起拷进去 | 数据库密码被烤进镜像层，`docker history` 就能读出来 | `.env` 被 `.gitignore` 挡住了，容易以为它不会进镜像 |
| 用了 `host.docker.internal` 但没配 `extra_hosts` | Linux 上直接 `Name or service not known`，聊天功能全废 | Mac/Windows 的 Docker Desktop 自带这个别名 |
| `TEMPERATURE` 等写在 `.env` 里却没在 compose 透传 | 改了参数毫无效果（静默失效） | compose 的 `.env` 只做文件内插值，不注入容器 —— 不看文档想不到 |
| 用 `MYSQL_CHARSET` 环境变量配字符集 | mysql 官方镜像不支持该变量，被静默忽略 | 不报错，且 MySQL 8 默认恰好也是 utf8mb4 |

---

## 🧪 运行测试

```bash
# 全部测试（143 个用例，无需 Ollama / MySQL / Redis；界面测试用无头方式跑）
python -m unittest discover tests -v
```

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_chatbot.py` | 领域层：Ollama 客户端、聊天逻辑、工具函数（31 个） |
| `tests/test_api.py` | HTTP 层：路由、参数校验、游标分页、404/422、就绪探针、目录接口、清空消息 |
| `tests/test_api_client.py` | 前端客户端：SSE 分片解析、**读取粒度回归**、错误映射、连接释放 |
| `tests/test_chat_session.py` | 会话抽象层：两种数据源的语义差异、失败降级 |
| `tests/test_app.py` | 界面层：用 Streamlit `AppTest` 无头执行 `app.py`，验证首屏与数据源切换 |
| `tests/test_cache.py` | 缓存层：TTL、主动失效、Redis 不可用时的降级 |
| `tests/test_deploy_manifest.py` | 部署清单自洽性：从源码反推容器必须拿到的环境变量、.dockerignore 覆盖密钥、healthcheck 与 depends_on 对齐 |
| `tests/test_deploy_manifest_guards.py` | 元测试：把每个要防的缺陷种回去，确认上面那些守卫真的会报错 |

> `tests/test_app.py` 是这一轮新增的能力：Streamlit 应用以前被认为「没法测」，
> 现在用官方 `AppTest` 可以在无浏览器的情况下执行整个页面。它上线当天就抓到一个真问题——
> 切到「后端 API」数据源、而后端没启动时，标题栏取当前模型抛异常、整页变红。
> 修完之后，那条回归用例降级进了 `tests/test_chat_session.py`（毫秒级）。
> **界面测试负责发现页面级问题，发现之后就该把它降级成单元测试守住。**

### CI

`.github/workflows/ci.yml` 在每次 push / PR 时跑：语法检查 → 单元测试，Python 3.11，期望 **143 passed**。

CI 里刻意**只装 `requirements.txt`**（不含 torch），并有一条 guard 步骤会在 torch 意外出现时直接失败：
装了 torch 的话每次 run 要多下 2–3GB，这正是「本地跑通 ≠ CI 跑通」最常见的坑。

另有一条反向 guard：**确认 `streamlit` 真的装着**。因为界面测试在没有 streamlit 的机器上
会 `skip` 而不是 `fail` —— 如果哪天依赖被误删，这些用例会安静地全被跳过，
「CI 是绿的」就成了假象。**跳过不等于通过，所以要专门守一道。**

---

## 🔧 技术栈

| 技术 | 说明 |
|------|------|
| Web 框架 | Streamlit（前端） / FastAPI（后端 API） |
| 大模型推理 | Ollama（本地 LLM 服务） |
| 数据库 | MySQL 8.0 + SQLAlchemy ORM |
| 缓存 | Redis（会话上下文缓存） |
| 深度学习框架 | PyTorch |
| 情感模型 | BiLSTM（双向长短期记忆网络） |
| HTTP 客户端 | requests |
| 配置管理 | python-dotenv + dataclasses |
| 测试 | unittest + mock |
| CI | GitHub Actions（ubuntu-latest / Python 3.11） |
| 容器化 | Docker + Docker Compose |

---

## 📄 License

MIT License
