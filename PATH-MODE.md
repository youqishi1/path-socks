# Path方案说明（双方案菜单中的选项2）

客户端使用 VLESS + WebSocket + TLS 连接本项目，由客户端Path指定公网SOCKS5落地。单个静态Go核心直接处理TLS，不需要Nginx、Node、Docker或workerd。

## 新版端口行为

- 默认尝试 TCP **25443**。安装时被占用则从20000—29999中选择空闲端口，跳过系统临时端口范围。
- 支持手填10240—65535；手填端口被占用会报错，不停止占用者。
- 端口保存在VPS，重启不自动换号；安装升级优先沿用本服务原端口。
- 不监听80、443或旧内部端口18080，也不启动、停止、重载系统Nginx。
- 一个端口同时服务所有用户/设备，不需要一台设备一个端口。
- 检查和启动之间仍可能被其他程序抢占；启用失败尝试恢复旧版本，不能承诺永不冲突。

## 安装与旧版升级

使用root登录SSH。准备以下内容：

1. 域名A记录指向本VPS公网IPv4，Cloudflare设为**仅DNS（灰云）**。本版监听IPv4，请移除该专用域名的AAAA记录。
2. 在Cloudflare个人资料 → API令牌 → 创建令牌 → “编辑区域DNS”，资源只选择所用域名所在的Zone。只给必要的DNS编辑权限，不使用Global API Key。
3. 在云厂商安全组/防火墙放行最终选择的TCP端口；安装器无法修改阿里云等厂商的云安全组。

[Cloudflare创建Token说明](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)；
[Certbot Cloudflare DNS验证与权限说明](https://certbot-dns-cloudflare.readthedocs.io/en/stable/)。

下载成功后才执行（新装与旧版迁移都用这一条）：

```bash
f=$(mktemp /tmp/path-socks-bootstrap.XXXXXX) && curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 https://raw.githubusercontent.com/youqishi1/path-socks/main/bootstrap.sh -o "$f" && sh "$f"
```

依次输入域名、端口（通常直接回车）、Cloudflare Token（隐藏输入）。Token不要发到聊天中。接受Let's Encrypt服务条款后，DNS验证临时创建/删除TXT记录，不使用80/443。通常需要等待至少60秒验证。

旧版注意：

- **不要用旧版菜单8跨架构升级**；请重新执行上面命令。新版服务模板更名，旧更新器会在下载阶段停止。
- 保留已有 `/etc/path-socks/users.db` 中的UUID；生成root私有的 `/var/backups/path-socks.*` 回滚备份。
- 启用新版会短暂断开代理连接。客户端必须把原443改成新版显示端口，UUID与Path不用改。
- 现有Nginx和旧项目站点配置原样保留，不会误停其他网站。旧站点仍可能指向停用的18080；需要原站点管理员另行清理。
- 原有Certbot HTTP验证续期任务不属于新版，本安装器不更改它们；若仍报旧站点续期错误，需要另行处理。
- 新版证书放在 `/etc/path-socks/acme`，只由本项目的独立续期任务处理。

## 管理和本地客户端

```bash
sbb
sbb show
sbb status
```

菜单11可改TLS端口，先检查是否空闲；启用失败会恢复原端口配置。成功后所有客户端都要改端口。不会自动删除旧防火墙规则。

客户端填写：

- 地址、SNI、Host：自己的域名。
- 端口：`sbb show`显示的实际值，不是固定443。
- VLESS；加密none；传输ws；TLS开启；保持证书验证，不要启用跳过验证。
- Flow留空；Mux关闭。
- 最大早期数据2048；早期数据头 `Sec-WebSocket-Protocol`。

Path示例（请换成真实信息）：

```text
/proxyip=socks5://用户名:密码@住宅IP:端口
/proxyip=socks5://住宅IP:端口
```

修改本地Path并重新连接即可切换SOCKS5，不需要在VPS维护代理列表。账户密码中的特殊字符要按客户端的URL编码规则处理。

## 证书、隐私和续期

- Token只存于VPS的 `/etc/path-socks/cloudflare.ini`，root可读写，不提交GitHub、不作为命令行参数。
- UUID和TLS密钥仅root及服务用户可读，程序目录由root拥有。
- 使用独立Certbot配置目录，systemd每日定时检查；Alpine/OpenRC使用daily任务。不会重启Nginx。
- 新证书原子替换后，核心在一分钟内热加载；已建立连接不因换证书断开。加载失败保留上一张有效证书并记录错误。
- 默认不记录访问Path，但本地客户端日志、分享链接和屏幕截图仍可能泄露SOCKS5密码。
- 客户端到VPS使用TLS；**VPS到普通SOCKS5服务器的认证本身不加密**，TLS不等于整个链路全部加密。
- 不回落到VPS直连；阻止连接本机和私网SOCKS5入口。
- 核心仅支持TCP、拒绝UDP；这并不能保证客户端其他程序不会通过系统直连DNS/UDP，需要客户端正确配置全局/TUN和DNS。

## 系统与性能边界

提供amd64和arm64静态二进制。安装脚本识别apt、dnf/yum、apk、zypper和systemd/OpenRC。
目标为Ubuntu/Debian、Fedora/RHEL系、Alpine、openSUSE；各发行版仓库的Certbot插件可用性不同，缺包时会停止，需要启用相应软件源。并非每个发行版都已真机验证。

自动测试包括32路TLS WebSocket经SOCKS5往返、端口占用检测、证书热加载及无效配置拒绝。它不是20台真实设备的长时间压力测试；实际可用人数、吞吐和延迟取决于CPU、总带宽、连接数与住宅代理质量。

高位端口不是提速技术，可能被部分接入网络限制。Cloudflare普通橙云不转发任意高位端口，请保持灰云；详情见[Cloudflare端口范围](https://developers.cloudflare.com/fundamentals/reference/network-ports/)。

本版不改全局拥塞算法/队列、不批量重载sysctl，不修改其他应用的网络配置。
