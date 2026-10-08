# Cloudflare Tunnel / 外部反向代理

Cloudflare Tunnel 的访问链路是 **浏览器 HTTPS → Cloudflare → cloudflared → 本机 HTTP 应用**。浏览器使用 HTTPS 时，Tunnel 服务地址填写 `http://127.0.0.1:8001` 是正常配置，不需要在本机 HTTP 应用上再配置 TLS。

## v0.5.2 的自动识别

cloudflared 运行在同一台宿主机、转发到本机应用端口时，新版会识别来自本机或 Docker 宿主网关的 `X-Forwarded-Proto` / `CF-Visitor`，还原浏览器使用的 HTTPS 来源。即使初始化时 `APP_PUBLIC_URL` 仍是本机 HTTP 地址，也不会因此误报“需要安全连接”。

HTTPS 校验、Origin/CSRF 校验及 Secure Cookie 使用同一个外部来源，因此登录后保存设置、备份、退出等操作也可正常进行。原有的直接 HTTPS、Caddy、SSH 隧道和本地更新健康探针继续兼容。

Tunnel 使用默认的 HTTP Host 设置即可。程序使用实际 Host 校验浏览器 Origin，不根据客户端提供的 `X-Forwarded-Host` 或 `Origin` 反推可信域名。如果另外配置了 Host 覆写，请将 `APP_PUBLIC_URL` 设置为真实公网 HTTPS 地址。

## 旧版本已被挡在登录页时

v0.5.1 及之前可先修正现有部署目录的 `.env`，恢复网页登录，再通过“版本更新”升级。以下域名只是示例，替换为自己的 Tunnel 公网域名：

```dotenv
APP_PUBLIC_URL=https://signin.example.com
APP_ENABLE_HTTPS_PROXY=0
APP_BIND_HOST=127.0.0.1
```

保留原有 `APP_PORT`、账号、密钥及其他配置。在该部署目录执行：

```bash
sudo docker compose -p any-signin-assistant up -d --no-build --force-recreate app
```

环境变量变化需要重新创建 app 容器，单纯 `restart` 不会重新读取 Compose 环境变量。以上操作复用当前镜像和数据；`APP_ENABLE_HTTPS_PROXY=0` 表示使用外部 Tunnel，避免以后维护时额外启动内置 Caddy。Tunnel 的本机 HTTP 服务地址保持原样。

## cloudflared 运行在另一个容器或主机

默认只信任实际 TCP 对端为本机回环或当前 Docker 宿主网关的转发头，不自动信任整个内网。若 cloudflared 从其他容器地址连接，明确配置其地址或专用小网段，例如：

```dotenv
APP_TRUSTED_PROXY_IPS=127.0.0.1,::1,gateway,10.12.0.5/32
```

地址替换为自己的代理对端；`gateway` 随 Docker 网络自动识别，不必填写固定网关。`none` 可关闭自动代理头信任，不支持 `*` 或全网 `/0`。修改后重新创建 app 容器。

应用端口应继续仅绑定本机或限制为代理可以访问的网络。不要把回源端口直接开放给公网。外部客户端伪造 HTTPS 转发头、客户端 IP 或转发 Host，不能绕过可信代理范围、登录和 CSRF 检查。
