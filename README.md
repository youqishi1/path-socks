# Path SOCKS5 轻量网关

轻量静态核心将VLESS + WebSocket + TLS流量转发到客户端Path指定的公网SOCKS5代理。运行时不需要Node、npm、Docker或workerd。

## 一键安装或升级

准备工作：

- 域名A记录已经指向VPS公网IPv4。
- Cloudflare代理状态为“仅DNS”（灰云）。
- VPS安全组已经放行TCP 80和443。

使用root登录SSH后执行：

```bash
curl -fsSL https://raw.githubusercontent.com/youqishi1/path-socks/main/bootstrap.sh | sh
```

根据提示输入域名。安装完成后输入 `sbb` 打开中文管理菜单。

支持：

- Ubuntu、Debian
- Fedora、CentOS、RHEL、Rocky Linux、AlmaLinux
- Alpine Linux
- openSUSE
- x86_64/amd64和arm64 CPU
- systemd和OpenRC

## 本地客户端

使用 `sbb` 查看VLESS参数和UUID。WebSocket Path：

```text
/proxyip=socks5://用户名:密码@住宅IP:端口
```

无认证SOCKS5：

```text
/proxyip=socks5://住宅IP:端口
```

切换落地IP只需修改本地客户端Path并重新连接，不需要修改VPS。

## 管理

```bash
sbb
```

菜单支持查看配置、添加用户、删除用户、重置UUID、更换域名、状态与内存检查、日志、在线更新和卸载。

## 轻量与稳定

- 单个约6MB的静态Go核心，无额外运行时。
- systemd/OpenRC自动拉起，TCP keepalive，TLS会话缓存。
- 文件句柄上限131072，20台以上设备的普通网页、办公和视频连接不构成程序并发瓶颈。
- 实际总速度仍由VPS带宽、中国到VPS线路及住宅SOCKS5质量决定。
- x86_64和arm64二进制均有SHA-256校验，提交后由GitHub Actions重跑测试。

## 隐私和防泄漏

- GitHub仓库不包含域名、UUID或SOCKS5凭据。
- UUID在每台VPS本机生成，文件仅root和服务账号可读。
- SOCKS5凭据不写入VPS配置文件，只随TLS加密的WebSocket请求传输。
- Nginx访问日志关闭。
- 只接受 `socks5://` Path；上游失败就关闭连接，不回落到VPS直连。
- SOCKS5入口必须解析到公网IP，阻止Path连接VPS本机或内网地址。
- UDP被拒绝，防止DNS或QUIC绕过SOCKS5；依赖UDP的游戏和语音可能不可用。
