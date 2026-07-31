# Contributing

1. 从 `main` 创建功能分支。
2. 不要提交缓存、Cookie、私有 API 密钥或测试下载内容。
3. 运行 `pytest`、`ruff check .` 和 `ruff format --check .`。
4. 新增解析逻辑时同时提交最小 HTML/JSON fixture，避免测试依赖在线网站。
5. Pull Request 中说明目标站点、内容类型、失败回退和合规限制。
