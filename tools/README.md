> 远程部署工具仅为可选示例。使用前设置 ANY_DEPLOY_SSH_HOST、ANY_DEPLOY_SSH_FINGERPRINT 和 ANY_DEPLOY_SSH_PASSWORD，并把脚本中的 signin.example.com 替换成自己的地址。

# tools/ — 部署与验证脚本

这些脚本只在本地电脑（D:\any-signin-assistant）上运行，不会进入 Docker 镜像。运行前需要本地 Python 虚拟环境（`.venv`，`pip install -r requirements.txt`）。所有临时文件写入项目内的 `.local/`（已被 .gitignore 忽略），脚本会在需要时自动创建它。

## 只保留项目文件

不要直接压缩整个工作目录。`.local/`、`.venv/` 和 `frontend/node_modules/` 是本地环境、缓存或验证产物，不是业务源码。

```powershell
# 只需要 Python 3.10+ 标准库；不依赖已安装的第三方包
python -B tools/export_source.py
# 验证导出范围与失败保护
python -B tools/verify_source_export.py
```

输出为 `.release/any-signin-assistant-v版本-source.zip`，包含前后端源码、锁文件、部署脚本、测试工具、文档和验收记录；不包含环境、缓存、数据库、密钥、部署密码、旧备份和测试结果。重新导出不会把上一次压缩包套进去，失败时保留原来的完整包。

这是方便保存和继续开发的源码 ZIP，不是在线更新附件。服务器更新仍用 `scripts/build_release.py` 生成的 `.tar.gz` 和 `release.json`，不要覆盖当前发布清单。导出不会删除原目录或改变服务器；源码包也不能替代含账号与密钥的私有备份。空间明细和清理边界见根目录 `项目文件与清理说明.md`。

## 部署到服务器（203.0.113.10）

`remote.py` 通过 SSH 连接服务器，已固定服务器主机指纹；密码通过环境变量 `ANY_DEPLOY_SSH_PASSWORD` 传入，不写在任何文件里。

```bash
# 1. 打包源代码（不含密钥、数据库、缓存）
.venv/Scripts/python.exe -B tools/prepare_release.py

# 2. 上传
ANY_DEPLOY_SSH_PASSWORD='服务器密码' .venv/Scripts/python.exe -B tools/remote.py upload .local/release.tar.gz

# 3a. 首次部署（服务器上还没有 /opt/any-signin-assistant）
ANY_DEPLOY_SSH_PASSWORD='服务器密码' .venv/Scripts/python.exe -B tools/remote.py run tools/server-deploy.sh
# 3b. 升级（已有部署：只替换源代码，保留 .env/data/secrets/backups，先自动备份再重建）
ANY_DEPLOY_SSH_PASSWORD='服务器密码' .venv/Scripts/python.exe -B tools/remote.py run tools/server-upgrade.sh

# 4. 查看进度 / 检查结果
ANY_DEPLOY_SSH_PASSWORD='服务器密码' .venv/Scripts/python.exe -B tools/remote.py run tools/server-status.sh
ANY_DEPLOY_SSH_PASSWORD='服务器密码' .venv/Scripts/python.exe -B tools/remote.py run tools/server-verify.sh

# 5. 下载部署信息（含随机管理员密码）到 D 盘
MSYS_NO_PATHCONV=1 ANY_DEPLOY_SSH_PASSWORD='服务器密码' .venv/Scripts/python.exe -B tools/remote.py download '/opt/any-signin-assistant/部署信息.txt' --destination 'D:\any-signin-assistant\部署信息.txt'
```

`server-inspect.sh` 只读取服务器信息（Docker 目录、已有容器、80/443 占用、防火墙），部署前先跑一遍。升级完成后队列处于暂停状态，请到网页点“继续队列”。

## 验证

| 脚本 | 作用 |
| --- | --- |
| `verify_backend.py` | 73 项后端合成数据检查（不联网、不用真实账号），结果写入 `verification-backend.json` |
| `verify_proxies.py` | 本机启动 HTTP / SOCKS5 / SSH 模拟节点，经程序的回环适配器连到 anyrouter.top，检查协议探测、指纹钉扎、白名单 |
| `verify_live.py` | 对 https://signin.example.com 做 14 项公网检查（读取本地 部署信息.txt 中的密码，不打印） |
| `verify_live_extract.py` | 在服务器上用一个不存在的合成账号跑完整提取流程（临时直连，结束后删除账号并恢复代理模式） |
| `verify_login_flow.py` | 本地用 Edge 驱动真实登录页跑一次提取流程（合成账号），验证页面适配 |
| `verify_real_account.py <账号> <密码> [代理行] [--checkin] [--edge]` | 本地用真实账号跑提取 / 检测 / 签到（默认用 .local/browsers 里的 Chromium，`--edge` 改用本机 Edge）；只打印分类和耗时 |
| `verify_live_auto.py <账号> <密码> <代理行>` | 在服务器上跑完整自动链路：导入 → 提取 → 自动加入默认计划 → 自动签到；打印每一步状态 |
| `probe_proxy.py` / `probe_exit_ip.py` / `probe_waf.py` / `probe_proxy_browser.py` | 排查代理与 WAF 行为：协议探测、出口 IP 是否轮换、验证 Cookie 是否与 IP 绑定、浏览器经代理加载登录页的时间线 |
| `inspect_login_page.py` / `time_login_flow.py` | 网站改版时用来查看登录页结构、给每一步计时 |

## 本地预览界面（可选）

```bash
# 一次性：初始化开发用密钥和数据库到 .local/dev（与服务器无关）
.venv/Scripts/python.exe -B -m app.manage init --root D:\any-signin-assistant\.local\dev --public-url http://localhost:5173 --port 5173
# 填充演示数据，然后分别在两个窗口启动后端与前端（需要 frontend/node_modules 和 .local/node.exe，或改用系统 node）
.venv/Scripts/python.exe -B tools/dev/seed.py
tools\dev\backend.cmd
tools\dev\frontend.cmd
# 用本机 Edge 截图各页面到 .local/dev/shots
.venv/Scripts/python.exe -B tools/dev/shots.py
```
