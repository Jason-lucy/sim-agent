# sim-agent — 自然语言仿真建模 Agent

> 把一段中文业务描述（**排队/服务流程场景**：客服中心、银行网点、医院门诊、餐饮门店…），端到端转成**可运行、可验证**的仿真模型，跑出确定性数字与图文报告。

**这是个人能力作品集项目。核心展示的是 Agent 工程能力：LLM 结构化输出的可靠性工程、基于评测集（eval）的迭代、真实部署。**

**定位（D-006）**：主攻**非制造业的商业/服务业场景**——中国商用 DES 近乎空白，且服务业容量决策对数据精度容忍度高，恰好放大「AI 把模糊描述变可跑模型」的价值。制造业产线场景为当前版本非目标。

---

## ⚠️ 个人项目合规声明

- 本项目为 **Jasson 的个人业余项目**，与本人任职的任何公司（含 Simul8 / Minitab）**无关**
- **不使用**任何公司资产、代码、数据、文档或商业机密
- **不支持**任何公司文件格式的导入/导出（如 `.sim8` 等）
- 所有代码与文档均为原创，基于开源组件构建（见下）

## 技术栈（全部开源）

| 层 | 组件 | 许可 |
|---|---|---|
| 仿真内核 | [SimPy](https://github.com/simpy/simpy) | MIT |
| 结构化输出 | Pydantic | MIT |
| LLM 接入 | openai SDK（OpenAI 兼容协议） | MIT |
| LLM 模型 | DeepSeek（开源权重，API 服务） | 开源权重 |
| 界面 | Streamlit | Apache 2.0 |
| 图表 | Plotly | MIT |
| 数据处理 | pandas | BSD |

## 架构（四层 + 三道防线 + 经验引擎）

```
自然语言场景描述
  │
  ├─【缓存层】场景指纹命中 → 0 次 LLM 调用秒回（代际号保证不命中旧代码结果）
  ├─【理解层】LLM + JSON Schema → 模型 spec（实体/资源/分布/规则）
  │            ← 注入：同类成功案例（few-shot）+ 历史避坑规则
  │            防线①：Schema 校验，不合规 → 错误回灌重问
  ├─【生成层】spec → SimPy 代码（参数化模板优先，LLM 兜底）
  │            防线②：模板优先，LLM 只填参数/选模板
  ├─【执行层】沙箱运行 SimPy → 结构化结果
  │            防线③：冒烟执行，报错回灌 LLM 自修复（≤3 次）
  ├─【解释层】结果 → 图表 + 结论报告
  │
  └─【经验写回】跑通且合理性验证无违规 → 进缓存/示例层；失败轨迹 → 规则层（人工确认）
```

**经验引擎（P2.5）**：系统随使用自改进——相同场景第二次秒回（0 次调用）；成功案例检索注入让抽取越来越准；失败教训提炼成规则让同类错不重犯。防污染闸门保证只有验证通过的运行才能当「示范」。正确率演进见 `evals/trend.html`。

## 快速开始

```bash
# 1. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 LLM API Key
copy .env.example .env        # 然后在 .env 中填入你的 Key

# 4. 最小闭环演示（P1 后可用；P2.5 起自动积累经验）
python m1_loop.py

# 5. 评测回归（P2 后可用；--replay-empty 跑空库对照）
python -m evals.run_evals
python -m evals.trend          # 正确率演进趋势图 → evals/trend.html

# 6. 网页界面（P3 后可用）
streamlit run app.py
```

## 在线部署（Streamlit Community Cloud）

1. 本仓库推送到 GitHub（公开）
2. [share.streamlit.io](https://share.streamlit.io) 用 GitHub 账号登录 → New app → 选仓库 → 主文件 `app.py`
3. App → Settings → Secrets，填入（对应 `.env.example` 的三项，云端没有 .env）：

```toml
LLM_BASE_URL = "https://api.deepseek.com"
LLM_API_KEY = "sk-你的key"
LLM_MODEL = "deepseek-chat"
```

> 云端文件系统只读：经验库自动降级为容器内的临时积累（重启清空），功能不受影响；本机运行的经验库（`experience/`）随 Git 分发，作为初始经验。

## 文档

- [决策日志](docs/decision-log.md) — 每个技术决策的理由与取舍
- [Non-goals](docs/non-goals.md) — 明确不做什么（纪律文件）
- [执行报告](docs/execution-report.md) — 执行情况与技术栈描述（P4 产出）

## 许可

MIT
