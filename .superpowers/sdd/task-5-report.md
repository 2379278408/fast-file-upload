# Task 5 实施报告

## 状态

已实现 complete route、服务端有界 assemble 与 SHA-256、durable publication states、文件发布与数据库消息事务、幂等 complete replay、启动 recover、missing confirmed part reconciliation、取消/过期清理及 maintenance 集成。legacy `process_upload()` 路径保持原有行为。

## TDD 证据

### 初始状态

- 简报定向命令在接手 worktree 时为 `9 passed, 56 deselected`，表明 worktree 中已有 Task 5 未提交实现，原始缺失 route 红灯已无法从当前工作树复现。

### 新增红灯

- `test_restart_uses_file_published_state_when_confirmed_part_is_missing`、`test_expired_unrecoverable_assembled_session_becomes_expired`、`test_cancel_file_published_session_removes_unavailable_final_file`：`3 failed`。
- `test_restart_finishes_cancelled_file_published_cleanup`、`test_expire_returns_recovery_mutation_for_maintenance_broadcast`：`2 failed`。
- `test_complete_preserves_sanitized_filename_metadata`：`1 failed`，complete 返回 `409`。

### 绿灯

- 三项 publication/expiry/cancel 边界测试：`3 passed`。
- 五项故障恢复边界测试：`5 passed`。
- 文件名 metadata 测试：`1 passed`。
- `tests/test_resumable_upload_api.py tests/test_upload_repository.py tests/test_files.py` 全量：`145 passed`。

## 故障注入证据

- assembly 后持久化失败：数据库停留 `assembled`，文件不可下载，重启收敛为一个永久消息。
- final rename 后失败：数据库停留 `assembled`，重启识别最终文件并继续 publication。
- database finalization 失败：数据库停留 `file_published`，最终文件在 `files/messages` 事务提交前不可下载，重启原子完成消息。
- `file_published` 下 confirmed part 丢失：恢复使用已验证最终文件，避免临时 part 对账破坏 durable publication。
- 取消后的 filesystem cleanup 失败：状态保持 `cancelled`，启动恢复继续清除未入库最终文件。
- 不可恢复且已过期的 `assembled`：恢复后仍按恢复前过期资格转为 `expired` 并发出事件。
- maintenance publication recovery：`expire()` 返回 `message.created` mutation，供现有 hub 广播。

## 自审

- 最终文件下载依赖 `files/messages` 数据库记录，`assembled` 和 `file_published` 文件均无法通过下载 route 访问。
- `finalize_publication()` 在单个 SQLite 事务中创建 file、message、关联 upload session 和 events；complete replay读取同一永久消息。
- startup recovery 不依赖 `SessionData`，使用 session 中持久化的 source device metadata。
- lifespan 先执行 resumable recovery，再执行 legacy reservation importer，避免 legacy importer 将 resumable 最终文件解释为 legacy 中断上传。
- assemble 使用 `max_concurrent_chunk_handlers` 限流；协程取消时等待线程文件操作结束后释放并发槽。
- server-computed SHA-256 作为 `file_sha256` 和永久 file metadata 的权威值。
- 连续空格文件名统一使用现有 `sanitize_filename()` 结果，避免 assemble 与数据库 metadata 分歧。

## Commit

- `feat(upload): add recoverable verified publication`（本报告随该 commit 提交）

## 顾虑

- 测试环境持续报告 Starlette 关于 `httpx` 兼容层的弃用警告。
- 仓库环境未安装 Ruff；静态验证使用 `compileall` 与 `git diff --check`。

## 高中严重性发现修复追加

### 红灯证据

- 并发 complete 与精确事件传播：`2 failed`。第二个 complete 启动第二次 assemble；interleaved message event 被 sequence 窗口重复广播。
- maintenance 锁与到期语义：`2 failed, 1 passed`。maintenance 在 assemble 期间重置 session；未到期 verifying 被全局 recover 提前发布。

### 修复内容

- `complete()` 的 keyed upload lock 覆盖 assemble、publish、database finalize 和临时清理；进程内 completion ownership 让并发 HTTP complete 立即返回 `409`。
- `begin_completion()` 仅接受持久化 `uploading`；`verifying` 的 `assembling`、`assembled`、`file_published` 均由 startup 或到期 maintenance recovery 继续处理。
- `complete()` 返回精确 mutation envelope，`result` 为永久 message DTO；route 仅广播该 envelope 的 events，replay 返回空 events。
- `recover()` 启动时枚举全部 session，并逐 upload 获取同一 keyed lock；part reconciliation、publication recovery 和文件清理均在该锁所有权下执行。
- `expire()` 仅查询到期非终态 ID，逐 upload 加锁后重读 status 与 expiry；仅到期 verifying 执行对应 publication recovery，且不再调用全局 recover。
- 活跃 part writer 使 recover/expire 跳过该 upload，避免 incoming/part 文件与 put 并发清理。
- lifespan 和 maintenance 直接 await 异步 recover/expire，并逐 envelope 精确广播持久事件。
- Task 5 brief 已同步 async recover/expire 与 complete mutation envelope 内部契约。

### 并发与事件证据

- 两个真实并发 complete 请求：一个 `200`、一个 `409`，assemble 与 finalize 均严格调用一次。
- maintenance 与已生成 `final.uploading` 的 assemble 并发：maintenance 等待 upload lock，文件保持存在，complete 收敛后 maintenance 重读为终态并跳过。
- 未到期 `verifying/assembled`：maintenance 返回空 mutation，状态与 assembled 文件保持。
- 到期 `verifying/assembled`：maintenance 在 upload lock 内完成 publication，返回永久 message mutation。
- interleaved text message：其 event 仅广播一次，complete 仅广播自己的 `upload.completed`、`message.created`、`file.finalized`；complete replay 无广播。
- durable `assembling`、`assembled`、`file_published` 的 HTTP complete replay 均返回 `409`，由 recovery 继续。

### 追加 Commit

- `fix(upload): serialize recovery and completion events`（本追加报告随新 commit 提交）

## 逐 Upload 异常隔离追加

### 红灯证据

- recover 首个 session 的 `discard_assembled()` 抛出 `OSError` 时，循环直接终止，第二个 session 保持 `verifying`。
- expire 首个 session 的 `expire_one()` 抛出 `sqlite3.Error` 时，循环直接终止，第二个 session 未过期。
- startup recover 首个 session 抛出意外 `RuntimeError` 时，FastAPI lifespan 启动失败。

### 修复内容

- recover、orphan cleanup 和 expire 均逐 upload 在 keyed lock 内运行，并对会话级故障隔离。
- 预期领域异常、`OSError`、`sqlite3.Error` 与意外 `Exception` 记录 `upload_id`、`phase` 和 traceback 后继续下一 ID。
- `CancelledError` 等待当前线程操作收束并继续传播，确保 keyed lock 不会在线程仍修改该 upload 时提前释放。
- 返回 mutation 仅包含成功处理会话；故障会话不追加成功结果，也不执行后续清理。

### 验证证据

- 新增 recover、expire、startup 故障注入测试全部通过，日志断言包含故障 upload ID 和 recover 阶段。
- Task 5 API、repository、files、events 测试：`164 passed`。

### 追加 Commit

- `fix(upload): isolate recovery failures per session`（本追加报告随新 commit 提交）
