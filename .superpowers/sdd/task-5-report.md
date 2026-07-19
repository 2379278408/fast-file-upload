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
