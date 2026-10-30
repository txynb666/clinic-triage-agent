# 参与开发

## 开发环境

```bash
bash scripts/init.sh      # 一键初始化（虚拟环境 + 依赖 + .env）
source .venv/bin/activate
make dev                  # 热重载启动
make test                 # 跑约束回归测试
```

## 目录约定

| 目录 | 放什么 | 不要放什么 |
|---|---|---|
| `app/core/` | 配置、常量 | 业务逻辑 |
| `app/api/` | 路由与请求校验 | 业务规则（放 services） |
| `app/services/` | 业务逻辑、工具实现 | HTTP 相关代码 |
| `app/agents/` | 编排循环、提示词 | 直接操作数据库 |
| `app/rag/` | 检索与向量化 | 业务规则 |
| `app/repositories/` | 数据访问 | 业务判断 |
| `static/` `templates/` | 前端资源 | 后端逻辑 |

## 提交规范

采用 Conventional Commits：

```
feat: 新增号源批量导入
fix: 修复令牌超时判断错误
docs: 补充部署说明
test: 增加并发抢号用例
refactor: 抽取 schema 初始化
```

## 改动智能体或工具时的硬性要求

1. **改工具必须跑测试**：`make test`，特别是顺序约束相关 9 项。
2. **改提示词铁律**：必须同步检查 `app/agents/prompts.py` 的兜底逻辑是否仍生效。
3. **新增写操作工具**：
   - 加入 `tools.WRITE_TOOLS`
   - 若需人工确认，加入 `harness.CONFIRM_REQUIRED`
   - 必须带幂等键
4. **新增接口**：错误必须走统一错误码（`code` + 中文 `message`），不得直接抛 500。

## PR 检查清单

- [ ] `make test` 通过
- [ ] 新增/修改的接口有错误码覆盖
- [ ] 未提交 `.env`、`clinic.db`、`data/index/` 等运行产物
- [ ] 涉及 Agent 行为的改动，说明对 30 条评测用例的影响
