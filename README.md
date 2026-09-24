# File Hub · 文件中转站

适合 Windows、macOS 和 Linux 的轻量文件传输网页。运行端只需 **Python 3.10 或更新版本**，无需安装 Python 第三方依赖；接收端用浏览器即可。文件保存在运行程序的电脑上，原有 `shared_files` 目录可以继续使用。

支持局域网直连，以及临时 HTTPS 公网地址，让手机流量、家中 Wi-Fi 和办公室网络中的设备访问同一个文件中转站。

## 快速开始

先安装 [Python](https://www.python.org/downloads/)。Windows 安装时勾选 `Add python.exe to PATH`。

| 使用场景 | Windows | macOS |
| --- | --- | --- |
| 跨网络传文件 | 双击 `start_public_windows.bat` | 运行 `./start_public_mac.command` |
| 同一局域网传文件 | 双击 `start_windows.bat` | 运行 `./start_mac.command` |

macOS 首次运行如提示没有权限，在项目目录执行：

```bash
chmod +x start_mac.command start_public_mac.command
```

### 不同网络之间使用

1. 启动公网脚本，或在项目目录执行 `python start_public.py`（macOS/Linux 使用 `python3`）。
2. 首次启动会下载指定版本的官方 `cloudflared`，校验 SHA-256 后再运行。
3. 等待终端显示 `https://……trycloudflare.com` 地址与访问码，将它们分别发给接收方。
4. 对方在浏览器打开地址，输入访问码，即可上传、下载和管理共享文件。
5. 传输期间保持电脑开机、联网，保留运行窗口；按 `Ctrl+C` 停止。

无需配置路由器端口转发。公网地址每次重启可能变化；访问码默认保存在本机 `.filehub/access-code` 中，后续启动继续使用。

这个入口使用 [Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)，适合临时分享和测试，官方不提供可用性保障，当前限制为最多 200 个并发请求，超限返回 HTTP 429。使用双方的网络必须能访问 Cloudflare；企业防火墙、运营商限制或服务故障仍可能阻断访问。它不能保证在所有网络都可用，也不提供固定网址或关机后的访问能力。需要长期服务时，请使用下方的服务器部署方式。

```bash
# 只下载、校验公网组件，暂不启动服务
python start_public.py --install-only

# 使用其他端口，并关闭每日清理
python start_public.py --port 8888 --no-auto-cleanup

# 已有自行安装的可信 cloudflared 时，显式指定路径
python start_public.py --cloudflared /path/to/cloudflared

# 网络无法使用 QUIC 时，尝试 HTTP/2；默认由 auto 自动选择
python start_public.py --protocol http2
```

`--protocol` 可选 `auto`、`quic`、`http2`。指定自己的可执行文件时，由使用者确认其来源；内置下载校验只适用于启动器管理的版本。公网启动器固定监听本机回环地址，不接受 `--host`，其余文件服务参数可直接传入。

Windows 启动脚本自动查找 Python。如果安装了多个版本，可以用环境变量 `FILE_HUB_PYTHON` 指定可执行文件的完整路径，或把路径单独写在本机 `.filehub/python-path.txt` 中。

### 同一局域网使用

```bash
python lan_file_hub.py
```

本机访问 `http://127.0.0.1:8765`；其他设备打开终端显示的局域网地址，输入访问码。Windows 防火墙需要允许 Python 在专用网络中通信。局域网 HTTP 不加密，请在可信网络使用；公共网络使用 HTTPS 公网入口。

如果有多级路由器，把程序运行在各设备都能访问的上层网络电脑上；访客 Wi-Fi、AP 隔离和路由器防火墙可能阻止局域网直连。

## 使用与数据

- 拖放或多选文件上传，查看进度、速度和失败提示。
- 文件按小块传输，网络恢复后可在当前页面重试，已确认的分块无需重复上传。刷新/关闭页面或重启服务后，不承诺恢复未完成任务。
- 搜索和排序共享文件；手机与电脑使用同一个网页。
- 下载支持 HTTP Range；新上传文件记录 SHA-256，便于核对文件内容。
- 登录后可以上传、下载和删除共享文件。所有持有访问码的人共用同一个空间，没有个人目录或只读角色。

默认文件目录为项目中的 `shared_files`。将需要保留的文件另行备份；这个工具是临时中转站。

**沿用原项目设置：默认每天本机时间 `08:00` 清空共享文件。** 程序需要在清理时刻运行；页面显示当前清理设置。长期保留文件请显式关闭：

```bash
python lan_file_hub.py --no-auto-cleanup
```

自定义参数：

```bash
python lan_file_hub.py --port 8888 --data-dir ./my-files --cleanup-time 23:30
python lan_file_hub.py --help
```

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址；公网启动器固定使用 `127.0.0.1` |
| `--port` | `8765` | 服务端口 |
| `--data-dir` | `shared_files` | 共享文件目录 |
| `--cleanup-time` | `08:00` | 每日清理时间，使用运行端时区 |
| `--no-auto-cleanup` | 未开启 | 关闭每日清理 |
| `--max-file-size` | `2147483648` | 单个文件上限，字节（2 GiB） |
| `--storage-limit` | `21474836480` | 共享空间容量上限，字节（20 GiB） |
| `--upload-ttl` | `7200` | 未完成上传的过期时间，秒 |
| `--access-code-file` | `.filehub/access-code` | 自动生成访问码的存储位置 |
| `--trust-proxy` | 未开启 | 信任本机回环代理的 HTTPS 转发头 |

容量参数也可通过 `FILE_HUB_MAX_FILE_SIZE`、`FILE_HUB_STORAGE_LIMIT` 设置；文件目录和清理时间分别支持 `FILE_HUB_DATA_DIR`、`FILE_HUB_CLEANUP_TIME`。上传按 8 MiB 分块，文件总大小仍受上述上限和磁盘可用空间限制。

访问码也可通过环境变量 `FILE_HUB_ACCESS_CODE` 设置，至少 16 个字符。建议使用密码管理器生成随机值。修改环境变量后重启服务生效；不要把访问码提交到 Git。未设置环境变量时，由程序生成并保存在 `.filehub/access-code`。

程序通过会话 Cookie 保持登录，限制错误登录尝试，校验修改请求，并限制文件路径和上传容量。公网入口使用 HTTPS，文件流量经过 Cloudflare 和运行端；文件存储在运行端，**不属于端到端加密传输**。访问码保护共享空间，不替代文件备份或多人权限系统。

## 固定网址与长期运行

GitHub 仓库存放源代码；**上传到 GitHub 或开启 GitHub Pages，不会运行 Python 文件服务**。固定网址需要一台长期在线的服务器，以及指向该服务器的域名，或自行配置正式 Cloudflare Tunnel。

仓库提供 Docker 和 Caddy 示例。下面的 `compose.yaml` 面向 **Linux Docker Engine**：容器使用主机网络，文件服务只绑定 `127.0.0.1:8765`；Caddy 在同一台宿主机上运行，负责 HTTPS。这保证反向代理请求来自可信回环地址，不向公网开放 Python 端口。其他容器网络布局需要另行配置，不能直接照搬。

1. 在服务器安装 Docker Compose 和 [Caddy](https://caddyserver.com/docs/install)，克隆本仓库。
2. 在项目目录创建 `.env`，内容为 `FILE_HUB_ACCESS_CODE=你生成的长随机访问码`。限制此文件的读取权限。
3. 执行 `docker compose up -d --build`。
4. 把 `Caddyfile.example` 中的 `files.example.com` 改成你的域名，保存到 Caddy 的配置路径（官方 Linux 服务通常为 `/etc/caddy/Caddyfile`），检查后重载 Caddy。
5. 域名 DNS 指向服务器，允许入站 TCP 80/443，然后用 `https://你的域名` 登录。

示例采用 [Caddy 的 HTTPS 反向代理](https://caddyserver.com/docs/quick-starts/reverse-proxy)。`--trust-proxy` 只信任回环连接中的转发头；直接部署时不要把后端端口映射到公网。Compose 的 [host 网络](https://docs.docker.com/engine/network/drivers/host/) 示例以 Linux 为目标，Windows/macOS 日常使用请优先使用 Python 启动脚本。

Compose 默认关闭每日清理，文件持久保存在名为 `filehub_data` 的 Docker 卷中；停止或重建容器不删除卷。请备份数据卷，勿使用 `docker compose down -v`，除非确实要删除文件。镜像默认时区为 UTC；如自行启用定时清理，先确认容器时区。

```bash
docker compose logs --tail 100
docker compose down
```

也可以只在本机测试容器（此命令仅监听本机，未配置 HTTPS）：

```bash
docker build -t file-hub .
docker run --rm -p 127.0.0.1:8765:8765 -v filehub_data:/data -e FILE_HUB_ACCESS_CODE file-hub
```

运行前需在当前 shell 设置 `FILE_HUB_ACCESS_CODE`；镜像默认关闭每日清理。Docker/Caddy 配置为部署示例，实际域名、证书、防火墙及备份需在目标服务器验证。

## 开发与验证

应用使用 Python 标准库和原生 HTML/CSS/JavaScript，没有前端构建步骤。

```bash
python -m unittest discover -s tests -v
python -m compileall -q lan_file_hub.py start_public.py tests
node --check web/app.js
```

GitHub Actions 对 Linux 的 Python 3.10、3.12、3.14 和 Windows 的 Python 3.12 运行上述检查。Node.js 仅用于 JavaScript 语法检查，不是运行服务的依赖。

排查时先看终端日志：端口占用可用 `--port` 修改；公网地址失效需重新启动公网脚本并使用新地址；上传中断可保持页面打开后重试；服务重启后需重新登录。请勿把实际文件、访问码、`.env` 或 `.filehub` 提交到公开仓库。
