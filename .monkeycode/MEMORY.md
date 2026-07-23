# 用户指令记忆

本文件记录了用户的指令、偏好和教导，用于在未来的交互中提供参考。

## 条目

### 图片理解工具偏好
- Date: 2026-07-18
- Context: 用户指定后续识图任务的执行方式
- Instructions:
  - 识别或分析图片时，优先使用模型自身的图片理解能力。
  - 不调用 MCP 图片理解工具。

### UI 密度测试套件已知问题
- Date: 2026-07-23
- Context: Agent 在执行 UI 密度终验时发现
- Category: 排错调试
- Instructions:
  - `test_old_page_anchor_restoration_has_no_smooth_drift_and_focuses_fallback` 是间歇性失败，与滚动分页时序敏感，与 UI 密度改动无关
  - pytest 不支持 `--timeout` 参数（未安装 pytest-timeout），不要使用该参数
  - Browser E2E 测试使用 Chromium，单次完整运行约 4-5 分钟
  - 前端契约测试约 4 秒完成

### 项目构建与测试命令
- Date: 2026-07-23
- Context: Agent 在执行 UI 密度任务时发现
- Category: 构建方法
- Instructions:
  - 前端契约测试：`python3 -m pytest tests/test_frontend_contract.py -q`
  - 浏览器 E2E：`python3 -m pytest tests/test_browser_e2e.py -q`
  - 默认全量：`python3 -m pytest -q`
  - Python 语法检查：`python3 -m compileall -q app`
  - Git 空白检查：`git diff --check`
  - 项目使用 FastAPI + SQLite + 原生 ES Modules，不新增运行时依赖
