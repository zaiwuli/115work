# 115work

面向国内本机和 NAS 环境的 115 离线下载、云解压及本地解压工具。
复用 UnpackFlow 的 xtractr 解压实现，默认不需要安装 7-Zip。

当前为开发验收版本。真实 115 账号、NAS 文件系统和容器环境仍需联调；
镜像构建结果以 GitHub Actions 状态为准。

## 镜像发布

推送到 `main` 后，GitHub Actions 自动构建 amd64 和 arm64 镜像，
发布到 `ghcr.io/zaiwuli/115work`，标记为 `latest` 和提交版本。
实际可用状态以 Actions 构建结果为准。

```powershell
docker compose -f compose.image.yaml up -d
```

通过 `http://设备地址:5656` 直接访问，不需要配置可信网段或网关。
可在 `.env` 中设置 `WORK115_PORT` 修改宿主机端口。
任何能连接该端口的人均可管理后台，请勿映射到公网。
本地卷必须允许容器内 UID 1000 写入。

## 本机启动

需要 Python 3.12 及以上版本。重新构建界面时需要 Node.js 22。

```powershell
.\.venv\Scripts\uv.exe sync --frozen --extra dev --extra p115
cd frontend
npm ci
npm run build
cd ..
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 5656 --workers 1
```

打开 http://127.0.0.1:5656 即可进入后台，默认不需要后台密码或令牌。
免登录模式接受设备地址访问，并保留浏览器跨站请求检查。
115 登录凭据、压缩包密码和 MS 通知凭据与后台管理密码无关。

## 核心功能

- 离线链接登记、提交、状态查询及关联产物追踪。
- 115 云解压、结果校验及失败后的人工确认。
- 确认后通过 115 直链缓存原包，再用内置 xtractr 解压。
- NAS 或本地目录扫描、分卷识别、密码候选、递归解压及结果校验。
- 复用旧工具的 MS 通知协议。
- SQLite 持久保存任务、步骤、事件、下载进度和产物关联。

CD2 的连接、扫描、刷新、下载及协议实现已移除，不再是运行依赖。
旧数据库结构保留历史字段，避免升级时破坏记录。

## 目录与配置

默认数据目录为 `data`：

- `watch`：本地压缩包来源。
- `inbox/links`：链接文档收件箱。
- `cache`：确认后的原包下载缓存。
- `output`：解压输出。
- `config/115work.db`：SQLite 数据库。

环境变量使用 `WORK115_` 前缀，参见 `.env.example`。
本地解压辅助程序为 `bin/115work-extract.exe`；
使用 `scripts/build-extractor.ps1` 可重新构建，容器构建会自动编译。

程序运行在电脑上时，缓存默认落在电脑数据目录。
要缓存到 NAS，需在 NAS 上运行程序并配置其数据目录，或配置可写的 NAS 路径。
容器内来源路径必须已挂载且对程序可见。

中文操作与流程说明见 [使用说明](docs/使用说明.md)。
第三方许可证保持原文，不应删除或改写授权声明。
