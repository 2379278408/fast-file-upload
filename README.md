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

消息删除后有 30 秒撤销窗口。窗口结束后，后台 purge 才能永久删除关联文件和记录。

## 实时事件

`GET /api/events?after=<sequence>` 升级为 WebSocket。连接使用签名会话 cookie，按严格递增 sequence 推送 JSON 事件；断线重连时使用最后收到的 sequence 回放遗漏事件。

## API

- `POST /api/session`：使用访问令牌创建设备会话。
- `GET /api/session`、`DELETE /api/session`：读取或退出当前会话。
- `GET /api/health`：返回服务和存储统计。
- `POST /api/messages`、`GET /api/messages`：发送文本并分页读取时间线。
- `GET /api/search`：搜索消息。
- `DELETE /api/messages/{message_id}`：软删除消息。
- `POST /api/messages/{message_id}/restore`：在 30 秒窗口内撤销删除。
- `POST /api/messages/batch-delete`：批量软删除消息。
- `POST /api/upload`：上传文件并创建文件消息。
- `GET /api/files`：分页筛选文件消息。
- `POST /api/files/batch-download`：生成有总大小限制的临时 ZIP。
- `DELETE /api/files/{file_id}`：通过所属消息执行软删除。
- `GET /download/{file_id}`：下载当前会话可访问的文件。
- `GET /api/storage`、`GET /api/audit`、`GET /api/admin/summary`：存储与审计视图。
- `POST /api/maintenance/purge`：立即运行一次过期删除清理。
- `GET /api/events?after=<sequence>`：WebSocket 实时事件与断线回放。

静态资源、`POST /api/session` 和 `DELETE /api/session` 可在无有效会话时访问；其余 HTTP API、下载和 WebSocket 均要求有效签名会话。`DELETE /api/session` 可在无有效会话时幂等清理 cookie，重复调用仍返回成功。

## 后台维护

应用 lifespan 启动时执行 recovery：协调中断的上传 reservation，恢复超时的 purge claim，并运行一次 purge。服务运行期间按 `MAINTENANCE_INTERVAL_SECONDS` 周期继续执行 claim recovery 与 purge。批量下载 ZIP 会在响应结束、客户端断开或应用关闭时清理。

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

聚焦前端契约测试：

```bash
python3 -m pytest -q tests/test_frontend_contract.py
```

聚焦真实 Chromium E2E：

```bash
python3 -m pytest -q tests/test_browser_e2e.py
```
