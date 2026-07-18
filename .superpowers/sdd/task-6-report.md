# Task 6 Report

## Status

第二次审查修复已完成并提交，最新 commit 为 `4b3b8a8`；前序 Task 6 commits 为 `b1171c1`、`66f9bc8`。

## Implementation

- 在 `tests/test_browser_e2e.py` 增加桌面 1440x900、移动 390x844、紧凑移动 390x568 三组真实浏览器覆盖。
- 每个视口覆盖 Transfer、Files、Manage 三路由，统一验证 hash、唯一可见 route page、document title、breadcrumb、route heading focus，以及桌面/移动双导航的 active 与 `aria-current` 一致性。
- 直接点击导航后聚焦 route heading；back/forward 后更新完整 route state，同时保持跨路由稳定控件的当前焦点。
- `navigation.js` 仅由 `navigate()` 设置 `focusNextRoute`，普通 history hashchange 不触发 heading focus。
- breadcrumb 末段增加 `data-route-title`，接入既有 navigation title 更新逻辑。
- 使用浏览器测试 API 上传真实文件，验证选中时 `#batchToolbar` visible、`pointer-events: auto`，清空与离开 Files 后 hidden、`pointer-events: none`，并在 toolbar visible 时验证 mobile nav 可点击；返回 Files 后 checkbox 保持取消且 toolbar 保持 inactive。
- toolbar class 校验使用 `classList.contains('visible')` token，并同步检查计算后的 visibility 与 pointer-events。
- 在每个路由验证 document 无水平 overflow。
- 390x568 seed 18 条消息，验证 timelineContainer 高度至少 120px、`overflow-y: auto`、内部 `scrollHeight > clientHeight`，且滚入可见位置后不被 composer/mobile nav 遮挡。
- 移动视口验证 composer 滚入可见位置后顶边不在视口上方、底边不与固定 mobile nav 重叠。
- 增加 Files 空状态 action E2E，通过 Playwright file chooser 事件验证返回 Transfer 并同步打开多文件 picker。
- 更新既有 skip-link E2E：保持当前 `#transfer` route hash，同时将焦点移至 `#mainContent`；进入 Files 后再验证视图 tabs。
- 旧分页 E2E 改用真实 API cursor 选择第二页 anchor，并隔离 WebSocket replay，消除 UUID 次级排序与 realtime race。
- 未引入新依赖。

## TDD Evidence

- Review red：`python3 -m pytest -q tests/test_browser_e2e.py -k 'three_route_navigation or mobile_batch_toolbar'` 为 4 failed，确认 breadcrumb 缺少 route title 接点。
- Review green：同一聚焦命令为 4 passed、6 deselected。
- Browser suite：`python3 -m pytest -q tests/test_browser_e2e.py` 为 10 passed。
- Full-suite debugging：完整 pytest 首次暴露旧分页测试的随机 anchor/realtime race；游标选取与 replay 隔离后浏览器套件和完整套件通过。
- Second review red：新增 QuickJS 与 E2E focus 测试为 4 failed、1 passed，记录 history 强制聚焦 heading 的生产问题。
- Second review green：navigation focus 与 batch 聚焦回归为 7 passed、90 deselected。

## Verification

- `python3 -m pytest -q tests/test_frontend_contract.py tests/test_browser_e2e.py`：97 passed，1 warning，31.33s。
- `python3 -m pytest -q`：276 passed，1 warning，36.52s。
- `python3 -m compileall -q app server.py tests`：exit 0。
- `git diff --check`：exit 0，无输出。
- warning 为既有 Starlette 与 `httpx` compatibility layer 弃用提示。

## Self Review

- 第二次审查的生产修复仅删除 `handleHashChange()` 中一行错误的 focus flag 写入。
- 三个 route 名与现有 HTML、JavaScript data attributes 完全一致。
- 每个 browser session 均继续执行 console、page error、CSP violation 与认证 401 白名单检查。
- seed 文件和消息使用每个 browser session 的 tmp storage/database，fixture 退出时隔离回收。

## Concerns

- 无阻塞项。
- 测试套件仍输出 1 条既有 Starlette/httpx 弃用 warning。
