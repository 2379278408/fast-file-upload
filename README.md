# Personal Transfer Timeline

基于 FastAPI、SQLite 和原生 JavaScript 的个人消息与文件传输时间线。所有数据 API、下载和 WebSocket 连接都使用签名会话鉴权。

## 安装与启动

运行时依赖：

```bash
python3 -m pip install --break-system-packages -r requirements.txt
```

`UPLOAD_TOKEN` 是 mandatory 配置；缺失、空值或纯空白值会使服务拒绝启动。

```bash
UPLOAD_TOKEN='replace-with-a-strong-token' python3 server.py
```

默认监听 `127.0.0.1:8083`。命令行可设置监听地址、端口、上传目录和单文件上限：

```bash
UPLOAD_TOKEN='replace-with-a-strong-token' python3 server.py --host 0.0.0.0 --port 3000 --dir /data/transfers --max-upload-size-mb 256
```

浏览器使用 `UPLOAD_TOKEN` 调用 `POST /api/session` 解锁。服务返回 HttpOnly、SameSite=Strict 的签名 cookie，会话有效期固定为 30 天。数据接口、下载与 WebSocket 后续只接受该会话 cookie，令牌不会加入下载 URL。

## 配置

| 变量 | 说明 | 默认值 |
|---|---|---|
| `UPLOAD_TOKEN` | 必填的解锁令牌 | 无 |
| `SESSION_SECRET` | 会话签名密钥；生产环境建议显式设置 | 由 `UPLOAD_TOKEN` 派生 |
| `UPLOAD_DIR` | 文件存储目录 | `./uploads` |
| `DATABASE_PATH` | SQLite 时间线数据库 | 上传目录同级的 `timeline.sqlite3` |
| `PORT` | 服务端口 | `8083` |
| `MAX_UPLOAD_SIZE_MB` | 单文件大小上限 | `512` |
| `MAX_BATCH_DOWNLOAD_TOTAL_BYTES` | 批量 ZIP 总字节上限 | `1073741824` |
| `ALLOWED_EXTENSIONS` | 允许的扩展名，逗号分隔；空值允许全部 | 空 |
| `ALLOWED_ORIGINS` | CORS 来源，逗号分隔或 `*` | `*` |
| `UNDO_SECONDS` | 软删除恢复窗口 | `30` |
| `MAINTENANCE_INTERVAL_SECONDS` | 后台维护间隔 | `60` |
| `PURGE_CLAIM_LEASE_SECONDS` | purge claim 超时恢复阈值 | `300` |
| `RETENTION_DAYS` | 兼容保留天数配置；当前 purge 流程不读取该值 | `0` |
| `LOGIN_RATE_LIMIT_COUNT` | 单客户端登录失败次数上限 | `5` |
| `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | 登录失败统计窗口 | `60` |
| `LOGIN_RATE_LIMIT_MAX_CLIENTS` | 登录限流器最多保留的客户端桶数量 | `1024` |
| `RATE_LIMIT_COUNT` | 上传和删除限流次数；`0` 关闭 | `0` |
| `RATE_LIMIT_WINDOW_SECONDS` | 上传和删除限流窗口 | `60` |
| `CLIENT_REQUEST_LOCK_CAPACITY` | 并发幂等请求锁的最大键数量 | `1024` |
| `UPLOAD_CHUNK_SIZE_BYTES` | 服务端拥有的分片边界；客户端可省略，显式声明时必须相同。配置最大 64MiB，且 `MAX_UPLOAD_SIZE_MB` 对应的分片数最多 10000 | `8388608` |
| `UPLOAD_SESSION_TTL_SECONDS` | 可恢复上传会话的闲置有效期 | `86400`（24 小时） |
| `UPLOAD_STORAGE_RESERVE_BYTES` | 创建和完成上传时必须保留的磁盘安全余量 | `268435456` |
| `MAX_ACTIVE_UPLOAD_SESSIONS` | 服务端允许的活动上传会话上限 | `128` |
| `MAX_CONCURRENT_CHUNK_HANDLERS` | 服务端并发分片请求处理上限 | `16` |
| `UPLOAD_PROGRESS_INTERVAL_SECONDS` | 单个上传进度事件的最小发送间隔 | `0.25` |
| `EVENT_RETENTION_LIMIT` | SQLite `events` 表保留的最新全局事件数量 | `10000` |

消息删除后有 30 秒撤销窗口。窗口结束后，后台 purge 才能永久删除关联文件和记录。

## 实时事件

`GET /api/events?after=<sequence>` 升级为 WebSocket。连接使用签名会话 cookie，按严格递增 sequence 推送 JSON 事件；断线重连时使用最后收到的 sequence 回放遗漏事件。

每条 message、file、upload 和 progress 事件均在写入事务内将 `events` 裁剪到最新 `EVENT_RETENTION_LIMIT` 条，周期维护提供额外校验。客户端 cursor 早于 retention floor 时，服务端发送 `{"event_type":"resync_required","sequence":<target>,"target_sequence":<target>,"reset_cursor":false}`；客户端 cursor 高于数据库最新 sequence 时发送相同控制协议并将 `reset_cursor` 设为 `true`，允许成功协调后向下重置 cursor。客户端必须完成时间线消息、活动上传和文件库的权威快照协调，成功后提交 target；协调失败时关闭当前连接 generation，并使用原 cursor 重连。事件回调跨 WebSocket generation 严格串行，旧 generation 完成的异步回调无法推进 cursor。

可恢复上传会发布 `upload.created`、`upload.progress`、`upload.state_changed`、`upload.completed`、`upload.cancelled` 和 `upload.expired`。进度事件包含上传 ID、状态、已确认字节、传输中字节、总字节、源设备和更新时间，并按 `UPLOAD_PROGRESS_INTERVAL_SECONDS` 节流；终态事件立即发布。

## API

- `POST /api/session`：使用访问令牌创建设备会话。
- `GET /api/session`、`DELETE /api/session`：读取或退出当前会话。
- `GET /api/health`：返回服务和存储统计。
- `POST /api/messages`、`GET /api/messages`：发送文本并分页读取时间线。
- `GET /api/search`：搜索消息。
- `DELETE /api/messages/{message_id}`：软删除消息。
- `POST /api/messages/{message_id}/restore`：在 30 秒窗口内撤销删除。
- `POST /api/messages/batch-delete`：批量软删除消息。
- `POST /api/upload`：legacy 整文件上传并创建文件消息。
- `POST /api/uploads`、`GET /api/uploads/active`、`GET /api/uploads/{upload_id}`：创建或恢复上传会话、读取活动会话和单个会话。
- `PUT /api/uploads/{upload_id}/parts/{part_index}`：上传并确认一个原始二进制分片。
- `PATCH /api/uploads/{upload_id}`、`DELETE /api/uploads/{upload_id}`：暂停、继续或取消上传。
- `POST /api/uploads/{upload_id}/complete`：流式组装、校验并原子发布文件消息。
- `GET /api/files`：分页筛选文件消息。
- `POST /api/files/batch-download`：生成有总大小限制的临时 ZIP。
- `DELETE /api/files/{file_id}`：通过所属消息执行软删除。
- `GET /download/{file_id}`：下载当前会话可访问的文件。
- `GET /api/storage`、`GET /api/audit`、`GET /api/admin/summary`：存储与审计视图。
- `POST /api/maintenance/purge`：立即运行一次过期删除清理。
- `GET /api/events?after=<sequence>`：WebSocket 实时事件与断线回放。

静态资源、`POST /api/session` 和 `DELETE /api/session` 可在无有效会话时访问；其余 HTTP API、下载和 WebSocket 均要求有效签名会话。`DELETE /api/session` 可在无有效会话时幂等清理 cookie，重复调用仍返回成功。

### 可恢复上传协议

`POST /api/uploads` 接收 JSON：

```json
{
  "client_request_id": "stable-request-id",
  "name": "archive.bin",
  "size_bytes": 41943040,
  "mime_type": "application/octet-stream",
  "last_modified_ms": 1784412345000,
  "sample_sha256": "64-character-lowercase-hex"
}
```

响应包含 `upload_id`、`status`、权威 `chunk_size_bytes`、`confirmed_parts`、`confirmed_bytes`、`source_device_id` 和 `expires_at`。请求可以省略 `chunk_size_bytes`；显式值与服务端配置不一致时返回带期望值的 `400`。相同 `client_request_id` 与元数据可安全重放；元数据冲突返回 `409`。空文件、超过 `MAX_UPLOAD_SIZE_MB`、扩展名受限或存储容量不足分别在接收分片前拒绝。容量准入在 SQLite `BEGIN IMMEDIATE` 事务内累计所有 active session 的未落盘字节和 assembly 副本峰值，新会话承诺 `2 * size_bytes`；已确认分片和终态会话相应减少承诺。需要重新 assembly 的 publication failure 会原子恢复该承诺并清除 durable publication 投影，磁盘安全余量始终保留。

每个 `PUT /api/uploads/{upload_id}/parts/{part_index}` 使用原始请求体，并携带：

```text
Content-Type: application/octet-stream
Content-Range: bytes <inclusive-start>-<inclusive-end>/<total-size>
X-Chunk-SHA256: <sha256-of-this-chunk>
```

服务端按创建响应返回的 `chunk_size_bytes` 校验索引、范围、长度和 SHA-256，随后原子确认分片。相同分片可幂等重放；相同索引的数据或摘要冲突返回 `409`。浏览器使用每个 session 的权威 chunk size 切片，每个文件仅发送一个在途分片，同时最多上传 9 个文件，其余文件保持 `queued`。

`PATCH /api/uploads/{upload_id}` 接收 `{"action":"pause"}` 或 `{"action":"resume"}`。暂停和继续仅允许创建会话的源设备执行；观察设备以只读方式接收状态，并可调用 `DELETE` 取消共享会话。完成接口仅允许源设备调用，要求所有分片连续覆盖文件；服务端以有界缓冲区组装并计算整文件 SHA-256，成功后返回永久文件消息。取消和完成后的非法状态转换返回 `409`，未知会话返回 `404`，容量或文件系统写入失败返回 `507`，并发资源耗尽返回 `503`。

会话状态为 `queued`、`uploading`、`paused`、`verifying`、`failed`、`complete`、`cancelled` 或 `expired`。创建后以及成功确认分片或成功改变状态时，会话闲置期限续期为 24 小时；过期会话由启动恢复和周期维护清理。

### 刷新恢复与重新选择

Transfer 页面刷新后先读取 `/api/uploads/active`，再与 IndexedDB 中的本地任务协调。浏览器可继续使用且仍获授权的文件句柄时，上传器只发送服务端尚未确认的分片。文件句柄不可用时，任务保持暂停并显示“重新选择原文件”；重新选择后会核对文件名、大小、最后修改时间和抽样 SHA-256，匹配后仅续传缺失分片，任何身份不匹配都会保持暂停并显示可操作错误。会话重新认证后执行相同协调流程。

### Legacy 迁移

`POST /api/upload` 是兼容已有消费者的 legacy `multipart/form-data` 整文件路由。Transfer UI 仅使用 `/api/uploads*`。移除 legacy 路由需同时满足两个标准：所有文档化消费者均已迁移到 `/api/uploads*`；可恢复上传的遥测与回归覆盖稳定运行至少一个发布周期。

## 后台维护

应用 lifespan 启动时执行 recovery：协调中断的上传 reservation，恢复超时的 purge claim，运行 purge，并裁剪事件保留窗口。服务运行期间按 `MAINTENANCE_INTERVAL_SECONDS` 周期继续执行 claim recovery、purge、上传过期和事件裁剪。批量下载 ZIP 会在响应结束、客户端断开或应用关闭时清理。

## 测试

测试依赖包含固定版本的 QuickJS 与 Playwright。安装 Python 测试依赖和 Chromium：

```bash
python3 -m pip install --break-system-packages -r requirements-test.txt
python3 -m playwright install chromium
```

Linux CI 镜像缺少 Chromium 系统库时，先安装对应系统依赖：

```bash
python3 -m playwright install-deps chromium
```

运行完整测试与 Python 编译检查：

```bash
python3 -m pytest -q
python3 -m compileall -q app server.py tests
```

默认 pytest 配置排除资源密集型 `large` 标记。显式运行稀疏 512MiB 分片、服务端 SHA-256 和 Python traced heap peak `<40MiB` 验证：

```bash
python3 -m pytest -q -m large tests/test_large_upload.py
```

内存指标由 `tracemalloc` 采集，仅覆盖 Python traced heap；RSS、native allocator 和 kernel buffers 位于测量范围外。in-process TestClient 会通过引用周期保留已完成请求体，因此测试在每个分片请求后执行 `gc.collect()`，让峰值反映逐请求回收条件下的服务端有界处理。

聚焦前端契约测试：

```bash
python3 -m pytest -q tests/test_frontend_contract.py
```

聚焦真实 Chromium E2E：

```bash
python3 -m pytest -q tests/test_browser_e2e.py
```
