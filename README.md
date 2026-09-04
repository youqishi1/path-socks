# Path SOCKS5 Gateway

VPS作为VLESS + WebSocket + TLS入口，每次连接使用客户端WebSocket Path指定的公网SOCKS5代理。

## 一键安装

准备工作：

- Ubuntu 24.04 x86_64 VPS。
- 域名A记录已经指向VPS公网IPv4。
- Cloudflare代理状态设置为“仅DNS”（灰云）。
- TCP 80和443已在服务商安全组中放行。

使用root登录SSH后执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/youqishi1/path-socks/main/install.sh)
```

根据提示输入域名。安装完成后输入 `sb` 打开中文管理菜单。

## 本地客户端

VLESS通用设置由 `sb` 菜单显示。WebSocket Path：

```text
/proxyip=socks5://用户名:密码@住宅IP:端口
```

无认证SOCKS5：

```text
/proxyip=socks5://住宅IP:端口
```

切换落地IP只需修改本地客户端Path并重新连接，不需要修改VPS。

## 隐私和防泄漏

- GitHub仓库不包含域名、UUID或SOCKS5凭据。
- UUID在每台VPS本机生成，存放于 `/etc/path-socks/users.db`，权限为600。
- SOCKS5凭据不写入VPS配置文件，只随TLS加密的WebSocket请求传输。
- Nginx访问日志关闭，错误日志只记录严重错误。
- 只接受 `socks5://` Path；上游失败就关闭连接，绝不回落到VPS直连。
- 自托管运行环境仅允许连接公网地址，阻止Path访问VPS内网和本机服务。
- UDP被拒绝，避免DNS或QUIC绕过SOCKS5；依赖UDP的游戏和语音功能可能不可用。

## 管理

```bash
sb
```

菜单支持查看配置、添加用户、删除用户、重置UUID、更换域名、状态检查、查看日志、在线更新和卸载。

## 技术说明

核心运行环境为Cloudflare开源的 [workerd](https://github.com/cloudflare/workerd)，使用其 `cloudflare:sockets` API建立SOCKS5出站TCP连接。
