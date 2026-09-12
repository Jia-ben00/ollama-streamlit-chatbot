# 🤖 Ollama Streamlit Chatbot

基于 **Python + Ollama + Streamlit** 构建的本地网页版聊天机器人。所有 AI 推理均在本地完成，数据不上传云端，保护隐私。

## ✨ 功能特性

- 💬 **流式对话** — 逐字输出 AI 回复，实时交互体验
- 🔄 **多模型切换** — 自动发现本地 Ollama 已安装的模型，一键切换
- ⚙️ **参数调节** — 可视化调节 Temperature、Top P、Max Tokens
- 🎭 **自定义角色** — 通过系统提示词定义 AI 的行为风格
- 📊 **对话管理** — 清空对话、导出 JSON 格式聊天记录
- 📈 **用量统计** — 实时显示消息数和预估 Token 消耗
- 🔌 **健康检测** — 侧边栏一键检测 Ollama 服务连接状态

## 🏗️ 项目结构

```
ollama-streamlit-chatbot/
├── app.py                  # Streamlit 主应用入口
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量示例
├── .gitignore              # Git 忽略规则
├── README.md               # 项目说明
├── src/
│   ├── __init__.py
│   ├── config.py           # 配置管理（环境变量 + dataclass）
│   ├── ollama_client.py    # Ollama REST API 客户端（支持流式/非流式）
│   ├── chatbot.py          # 聊天机器人核心逻辑（会话管理）
│   └── utils.py            # 通用工具函数
├── tests/
│   ├── __init__.py
│   └── test_chatbot.py     # 单元测试（mock 离线运行）
└── assets/                 # 静态资源
```

## 🚀 快速开始

### 前置条件

1. **安装 Ollama** — 从 [ollama.com](https://ollama.com/) 下载并安装
2. **启动 Ollama 服务**：
   ```bash
   ollama serve
   ```
3. **拉取模型**（以 llama3.2 为例）：
   ```bash
   ollama pull llama3.2
   ```
4. **Python 3.10+**

### 安装与运行

```bash
# 1. 克隆仓库
git clone https://github.com/your-username/ollama-streamlit-chatbot.git
cd ollama-streamlit-chatbot

# 2. 创建虚拟环境（推荐）
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量（可选）
cp .env.example .env
# 编辑 .env 修改模型、端口等配置

# 5. 启动应用
streamlit run app.py
```

启动后浏览器会自动打开 `http://localhost:8501`。

### 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama 服务地址 |
| `OLLAMA_MODEL` | `llama3.2` | 默认使用的模型 |
| `OLLAMA_TIMEOUT` | `120` | 请求超时时间（秒） |
| `TEMPERATURE` | `0.7` | 采样温度 |
| `TOP_P` | `0.9` | 核采样参数 |
| `MAX_TOKENS` | `2048` | 最大生成 token 数 |
| `APP_TITLE` | `Ollama 智能聊天助手` | 应用标题 |

## 🧪 运行测试

```bash
python -m pytest tests/ -v
# 或
python -m unittest discover tests -v
```

测试使用 mock 替代真实 API 调用，可在无 Ollama 环境下运行。

## 🔧 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | Streamlit |
| AI 推理 | Ollama（本地 LLM 服务） |
| 后端 | Python 3.10+ |
| HTTP 客户端 | requests |
| 配置管理 | python-dotenv + dataclasses |
| 测试 | unittest + mock |

## 📝 使用说明

1. 确保 Ollama 服务已启动并拉取了至少一个模型
2. 启动 Streamlit 应用后，在侧边栏点击「检测连接」确认服务状态
3. 在模型下拉框中选择要使用的模型
4. 可调节生成参数和自定义系统提示词
5. 在底部输入框中输入问题，AI 将流式回复
6. 使用侧边栏的「清空对话」或「导出记录」管理对话

## 📄 License

MIT License
