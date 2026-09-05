# LocalCareerImpactAgent

本地多智能体职业影响分析原型 / Local multi-agent career-impact analysis prototype.

通过对话、PDF/TXT 和音频采集职业资料，结合本地知识库，生成未来 **1–3 年、3–5 年**的任务变化、机会、风险与行动建议。Qwen3-VL-30B 负责所有语义决策，三个 Qwen3-VL-4B 审核角色提供建议，后端校验引用和结构后保存报告。

**当前状态：开发中的 MVP。** 已有真实本地 API 场景完成九阶段分析并保存有效报告；长资料和复杂输入的重复运行仍可能在生成或最终校验阶段失败。后台任务、SSE 和最终报告的前端展示尚待完成。当前页面支持对话、资料确认与知识库管理，完整分析通过后端执行接口触发。

## 核心设计

- **本地推理**：Apple Silicon / MLX；30B 主模型与共享 4B 审核模型独立进程驻留，由一个 Metal 信号量控制推理资源。
- **星形多 agent**：证据审核、推理边界审核、语言与安全审核均只有建议权；主模型逐条决定采纳或拒绝。
- **本地 RAG**：文档分块、SQLite FTS5 稀疏检索、向量相似度检索、RRF 排序融合；冻结知识快照，保持报告与来源绑定。
- **分阶段生成**：F1 决议、F2 修订主张、F3 报告叙事，分别使用小型结构化合同和有界重试。
- **确定性装配**：后端生成身份字段与引用、派生来源属性、验证主张关联；完整校验通过后才原子保存报告。
- **多模态资料输入**：文本、PDF 文本/OCR、本地 Whisper 音频转写及浏览器麦克风录音。
- **双语与访问控制**：英文消息默认英文报告，否则中文；手动 CSV 账号管理和本地会话。

```text
对话 / PDF / TXT / 音频
          ↓
资料抽取 → 用户确认 → 冻结 RAG 证据
          ↓
     30B 生成初稿
          ↓
共享 4B：证据审核 → 边界审核 → 语言与安全审核
          ↓
30B：F1 决议 → F2 修订主张 → F3 报告叙事
          ↓
确定性装配 → 完整校验 → SQLite 原子保存
```

## 技术栈

| 层次 | 实现 |
| --- | --- |
| 前端 | React 19、TypeScript、Vite |
| 后端 | Python 3.12、FastAPI、Pydantic |
| 存储与检索 | SQLite WAL / FTS5、dense retrieval、RRF |
| 本地大模型 | Qwen3-VL-30B-A3B-Instruct-4bit、Qwen3-VL-4B-Instruct-4bit、MLX VLM |
| Embedding | 本地 Transformers 编码器；示例配置为 all-MiniLM-L6-v2 |
| 文档与语音 | PyMuPDF、Tesseract OCR、MLX Whisper、FFmpeg |
| 环境管理 | uv，control / qwen / embedding / asr 四个隔离环境及锁文件 |

## 本地运行

目标平台为 Apple Silicon macOS，需要 Python 3.12.12、uv、支持当前 Vite 版本的 Node.js，以及足以同时容纳两个量化模型的统一内存。其他操作系统与低内存设备未验证。

依赖安装需要联网；运行时使用预先下载的本地模型。仓库不包含模型权重、知识数据、候选人资料或真实账号。

### 1. 安装依赖

在仓库根目录执行：

```sh
uv sync --project environments/control --locked
uv sync --project environments/qwen --locked
uv sync --project environments/embedding --locked
uv sync --project environments/asr --locked
npm --prefix web ci
npm --prefix web run build
```

音频处理当前使用 `/opt/homebrew/bin/ffmpeg`。PDF OCR 需要 PATH 中的 `tesseract`，并安装 `eng`、`chi_sim`、`chi_tra` 语言包。Whisper 应使用 MLX 兼容模型目录，Qwen 应使用 MLX 兼容的 4-bit 权重。

### 2. 配置模型和账号

复制 `config/models.local.json.example` 为 `config/models.local.json`，将四个目录替换为本机实际路径。复制 `config/users.csv.example` 为 `config/users.csv`，设置自己的密码并将需要的账号设为 `enabled=true`。知识库管理使用用户名 `admin`，需要在 CSV 中自行创建该账号。

账号文件使用简单明文 CSV，仅面向受控本地 demo。模型、账号、数据库和上传文件已在 `.gitignore` 中排除。

### 3. 启动

```sh
PYTHONPATH=src environments/control/.venv/bin/python -m uvicorn \
  localcareerimpact.app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://localhost:8000>。启动会加载两个 Qwen worker，首次就绪可能需要等待。使用单个服务器进程，避免多进程重复加载模型。前端开发可运行 `npm --prefix web run dev`，Vite 将 `/api` 代理至本地 8000 端口。

管理员先在知识库界面导入文档并完成重建；用户提交资料并确认后生成待执行 run。完整分析接口为 `POST /api/runs/{run_id}/execute`，需要该 run 所属用户的登录会话，当前会保持请求直至分析结束。前端自动执行、SSE 和最终报告卡片尚未接入。

## 验证与限制

已有 134 项检查覆盖环境与合同等基础行为；模型生成与端到端行为需另做真实本地验收。现有测试源码随快照原样保留，本次仓库整理未新增或修改测试。

历史真实 API 成功场景完成了三个 reviewer、F1/F2/F3、九条建议与九条决议匹配、同一快照内的两条 citation 解析、一个有效报告保存和 9/9 进度。后续稳定性运行仍存在草稿格式失败，以及长资料在装配或最终校验阶段失败的情况；一次通过不能视为稳定性或职业预测准确率证明。

本项目输出情景分析，不提供个人精确失业概率。当前适用于本地演示与工程探索。

```sh
# 需要先安装上述四个环境；环境检查依赖 Apple Silicon 和本地解释器。
PYTHONPATH=src environments/control/.venv/bin/python -m pytest -q
npm --prefix web run typecheck
```

## 目录

```text
src/localcareerimpact/
  agent/       主模型编排、审核、结构合同、装配与校验
  app/         FastAPI、账号、会话、聊天与数据库
  intake/      PDF、TXT、音频和资料抽取
  knowledge/   文档、向量、混合检索与快照
  workers/     本地 Qwen 进程协议与生命周期
  contracts/   底层证据和引用合同
web/           对话式前端
environments/  四套隔离 Python 环境
config/        不含个人数据的配置模板
docs/          项目经历与源码来源说明
tests/         既有环境与合同检查
```

本仓库是当前开发代码的独立发布快照，未上传本机调试记录与原始开发历史。来源与已知状态见 [源码说明](docs/SOURCE_SNAPSHOT.md)。中文求职表单文案见 [项目经历](docs/PROJECT_EXPERIENCE.zh-CN.md)。
