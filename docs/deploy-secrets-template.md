# Streamlit Community Cloud 部署配置（复制粘贴用）

## 部署参数

| 项 | 值 |
|---|---|
| Repository | `Jason-lucy/sim-agent` |
| Branch | `main` |
| Main file path | `app.py` |
| Python version | 默认即可（3.11/3.12 均可） |

## Secrets（部署时必填）

在 Streamlit Cloud 的 **App → Settings → Secrets** 里粘贴（把 Key 换成你的真实值）：

```toml
LLM_BASE_URL = "https://api.deepseek.com"
LLM_API_KEY = "sk-9be1d1ed1654493fabb760da312819db"
LLM_MODEL = "deepseek-chat"
```

> 注意：三项都要，缺 LLM_BASE_URL 会连不上 DeepSeek。
> Secrets 保存后 App 会自动重启，无需手动操作。
