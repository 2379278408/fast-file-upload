# Fast File Upload

一个经过升级的轻量文件上传工作台，基于 `FastAPI + 原生前端` 构建，强调三件事：上传体验、部署简洁、最小治理能力。

## 当前能力

- 流式上传，避免一次性把大文件读进内存。
- 支持批量上传队列与逐项进度反馈。
- 支持文件搜索、扩展名筛选、排序、下载、复制链接与删除。
- 暴露 `/api/health` 健康检查接口，返回统计信息与保护状态。
- 支持可选访问令牌 `UPLOAD_TOKEN`。
- 支持可选扩展名白名单与单文件大小限制。
- 拒绝空文件，自动收敛超长展示文件名。
- SVG 按文档文件处理，可下载但不会内联预览。
- 默认添加基础浏览器安全响应头，并避免在健康接口暴露本地上传目录。
- 支持上传/删除接口内存限流，降低滥用风险。
- 支持按保留天数自动清理过期文件。
- 文件响应包含 SHA-256，复制链接时会附带校验值。
- 提供审计日志导出与管理摘要接口。
- 自带 `pytest` 回归测试覆盖核心上传链路。

## 目录结构

```text
fast-file-upload/
├── app/
│   ├── config.py        # 配置解析
│   ├── main.py          # FastAPI 应用工厂
│   └── storage.py       # 文件存储与校验逻辑
├── server.py            # CLI 启动入口
├── tests/
│   └── test_app.py      # API 回归测试
├── web/
│   └── index.html       # 单页前端界面
└── requirements.txt
```

## 安装

```bash
python3 -m pip install --break-system-packages -r requirements.txt
```

## 启动

```bash
python3 server.py
```

自定义端口、目录和大小限制：

```bash
python3 server.py --port 3000 --dir /data/my-files --max-upload-size-mb 256
```

默认访问地址：`http://localhost:8083`

## 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `UPLOAD_DIR` | 文件存储目录 | `./uploads` |
| `PORT` | 服务端口 | `8083` |
| `MAX_UPLOAD_SIZE_MB` | 单文件大小限制，单位 MB | `512` |
| `ALLOWED_EXTENSIONS` | 允许上传的扩展名，逗号分隔，例如 `.pdf,.zip,.png` | 空，表示不限 |
| `ALLOWED_ORIGINS` | CORS 允许来源，逗号分隔或 `*` | `*` |
| `UPLOAD_TOKEN` | 可选访问令牌。设置后，上传、列表、下载、删除都需要携带令牌 | 未设置 |
| `RATE_LIMIT_COUNT` | 上传和删除接口的窗口内请求上限，`0` 表示关闭 | `0` |
| `RATE_LIMIT_WINDOW_SECONDS` | 限流窗口秒数 | `60` |
| `RETENTION_DAYS` | 文件保留天数，`0` 表示关闭自动清理 | `0` |

## API

- `GET /api/health`：健康检查与统计信息。
- `GET /api/files`：列出文件与统计信息。
- `GET /api/audit`：导出最近审计事件。
- `GET /api/admin/summary`：返回过大文件、旧文件和最大文件摘要。
- `POST /api/upload`：上传文件。
- `GET /download/{id}`：下载文件。
- `DELETE /api/files/{id}`：删除文件。

当启用 `UPLOAD_TOKEN` 后，前端会自动附带令牌。脚本调用可以使用这两种方式：

```bash
# Header 方式
curl -H "X-Upload-Token: your-token" http://localhost:8083/api/files

# 下载场景也支持 query 参数
curl "http://localhost:8083/download/<id>?token=your-token" -o file.bin
```

## 测试

```bash
pytest
```

## 适合场景

- 局域网或小团队内部交付文件。
- 临时项目资产收集。
- 需要简单治理而不想引入对象存储或数据库的场景。

## 治理建议

- 面向公网时优先配置 `UPLOAD_TOKEN`。
- 小团队临时分发建议设置 `RETENTION_DAYS=7`；项目资产收集建议设置 `RETENTION_DAYS=30`；需要长期归档时保持 `RETENTION_DAYS=0` 并定期人工复核。
- 自动保留期清理会在健康检查、文件列表读取和上传前触发。历史文件会按当前配置一起参与过期判断。
- 对已有存储目录启用保留期前，先通过 `/api/admin/summary` 查看旧文件与大文件，再决定是否开启清理。
- 如果需要更强治理，可以在反向代理层继续增加 Basic Auth、IP 白名单、TLS 与审计日志。
- 如果要承接大规模公共上传，建议进一步增加病毒扫描、限流与持久元数据存储。
- 如果允许用户上传 SVG，请保持当前下载态处理，避免把 SVG 作为页面内联图片预览。
