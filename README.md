# SBB 住宅代理中转：三种安装选择

一条SSH命令安装，菜单选择方案；以后输入 **sbb** 管理。新版本没有远程替你修改VPS，必须由你在目标VPS执行安装。

| 选择 | 用途 | 安装需要 | 住宅IP怎么换 |
|---|---|---|---|
| 1 · 直连VPS IP | REALITY节点链接或手填参数，客户端手动链式SOCKS5 | 公网IPv4、空闲高位TCP端口；无需自己的域名/CF Token | 在v2rayN住宅分组手动添加节点、点击切换 |
| 2 · Path原版80/443 | Nginx＋Go核心＋WebSocket TLS | 域名灰云、空闲且公网可达的80/443；不需要CF Token | 直接修改客户端Path |
| 3 · 其他/备用 | 子菜单1：高位Path；子菜单2：REALITY配置助手 | 高位Path需要域名/CF Token；助手需要生成文件导入 | 按对应备用方案操作 |

REALITY与Path配置独立，可同时保留。**两种Path模式共用核心和UUID，互相切换而非同时运行**；切换会短暂断开Path连接，需要更新客户端端口。原版检测到其他服务占用80/443时停止，不接管原网站。原版使用独立`path-socks-nginx`实例及配置；若本次首次安装Nginx软件包，会停用该新安装包的默认实例，已有其他Nginx服务不操作。

## 1. VPS安装

FinalShell以root登录后，整行复制：

```bash
sbb_file=$(mktemp /tmp/sbb-install.XXXXXX) && curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 https://raw.githubusercontent.com/youqishi1/path-socks/main/bootstrap.sh -o "$sbb_file" && sh "$sbb_file"
```

出现菜单：

```text
1. 直连VPS IP（节点链接/手填参数）
2. 连接域名（80/443，Path手填住宅）
3. 其他/备用方案（高位Path、配置助手）
0. 退出
```

选择1后：

1. 确认自动识别的公网IPv4；错误时手填正确地址。
2. 端口通常回车，优先26443；被占用时自动选择空闲高位端口。
3. REALITY目标域名通常回车。它不是你的域名；安装器会检查目标的TLS1.3与HTTP/2。检查失败先停止，不关闭证书验证。
4. 安装后，在阿里云等厂商的**安全组放行显示的TCP端口**。脚本不能替你改云安全组。
5. 默认生成10个独立用户，可在sbb添加。默认显示对应用户的`vless://`节点链接和手填参数，只交给该用户。无需配置助手。已有安装升级会保留密钥、UUID和端口，过程中可能短暂重连。

已有Path版本的用户：用上述命令进入新菜单，不要用旧版菜单8跨方案更新。选择1不会读取CF Token，也不会覆盖Path的UUID。已有旧版单方案管理脚本会保留。

选择2后：输入域名即可。提前将域名A记录指向本VPS、关闭橙云，在云安全组放行TCP **80和443**；不要保留错误的AAAA记录。自动申请HTTP-01证书，客户端使用443，后续继续直接改Path。80需要持续开放用于续期，不需要Cloudflare Token。其他服务已占用80/443则安全退出，不会强停；可以改选1或3。

REALITY服务：`sbb-reality`，程序 `/opt/sbb-reality`，配置 `/etc/sbb-reality`。Path服务：`path-socks`，目录沿用旧版。原版额外使用`path-socks-nginx`及`/etc/path-socks-nginx`，不覆盖`/etc/nginx`网站配置。不修改全局BBR、队列或其他网络参数。

## 2. 首选：v2rayN直接手填住宅，不生成文件

在VPS执行 `sbb reality show`，选用户，复制`vless://`完整一行，在v2rayN按Ctrl+V导入。该节点仅是VPS中转，不是住宅出口。

1. 将VPS节点放在单独分组，保留唯一备注（例如生成的`SBB-VPS-...`）。
2. 新建“住宅出口”分组，在该分组设置中的**前置代理别名**填写VPS节点完整备注，落地代理别名留空。
3. 在“住宅出口”分组手动添加SOCKS节点，填写代理商提供的住宅IP、端口、用户名、密码，选择Xray核心。VPS节点不要放进这个分组，避免循环引用。
4. 启用住宅节点。多个住宅就添加多个SOCKS节点，后续点击切换，不用生成JSON、不用Path。
5. 核对出口及失败行为：住宅密码错误应无法访问。别名错、改名或删除前置节点可能导致链式关系被跳过；不同版本界面可能不同，找不到设置先停止并核对版本，不要假定链式已经生效。

这是v2rayN客户端原生功能，VPS升级不能代替首次客户端设置。参见[官方链式代理说明](https://github.com/2dust/v2rayN/wiki/Description-of-proxy-chain)。原来的配置助手和已生成文件继续保留，不强制迁移。

## 备用：配置助手流程（可选，不作为默认步骤）

在VPS执行 `sbb reality helper` 获取旧式SBB1连接码，或进入 `sbb` → 3其他/备用 → 1配置助手连接码。

下载仓库里的 [client-helper.html](client-helper.html)（在GitHub文件页面选择下载原始文件），保存到电脑后双击打开。无需Python、Node或安装其他运行时。

1. 粘贴sbb导出的SBB1.连接码。
2. 填写住宅公网IPv4、端口、账号密码。
3. 选择客户端，生成配置。

**离线助手无网络请求、不写浏览器存储、不上传凭据。** 生成的文件仍包含账号密码；不应上传GitHub、公共订阅站点或聊天群。切换住宅时重新填写并生成即可，原连接码可沿用。当前助手要求住宅入口为公网IPv4，避免入口域名的启动DNS依赖。

### 电脑v2rayN

选择“v2rayN / Xray”，下载JSON。在v2rayN添加/导入自定义配置，核心选择Xray。文件使用本地SOCKS 10808、HTTP 10809；确认自定义配置端口与文件一致，且未被其他代理客户端占用。

此配置的唯一业务出口是住宅SOCKS5，该SOCKS5经VPS建立连接。不含住宅失败后直连的备用出站。不要同时启用另一套占用相同本地端口的客户端。

### 电脑Clash Verge Rev

选择“Mihomo”，导入下载的本地YAML文件并启用它。文件内容采用JSON格式（合法YAML），不需要你编辑。

使用**规则模式**，住宅出口组只含RESIDENTIAL。RESIDENTIAL的`dialer-proxy`固定指向VPS；不要切到全局模式后选VPS，否则出口就会变成VPS。Verge全局覆写/扩展脚本可能改变配置，使用前检查合并后的规则及DNS。

### 苹果Shadowrocket

选择“Shadowrocket”，助手显示中转链接和分步设置：

1. 导入REALITY中转节点，命名为VPS中转。
2. 添加住宅SOCKS5节点。
3. 在**住宅节点**设置中，把“代理通过”设为VPS中转并保存。
4. 最终选择住宅节点，不是VPS中转节点。

不同版本菜单位置可能不同；没有该设置时先核对版本或提供遮盖凭据的界面截图，不要直接把VPS节点当住宅节点使用。尚未在用户iPhone上实测。少量安卓设备也需支持REALITY与链式配置的客户端，不能保证任意安卓客户端都能直接导入。

## 3. 使用前必须检查

- 出口检测应显示住宅代理的实际出口，而不是VPS地址。部分供应商入口IP和出口IP不同，要以供应商说明为准。
- 暂停住宅代理或填错其密码后，访问应失败，不应仍然经VPS上网。
- 模板仅面向TCP；拒绝UDP。不适用于所有游戏、语音及QUIC场景。
- 系统代理不等于全设备VPN；不遵守系统代理的软件、客户端自带直连规则或系统DNS仍可能绕过。需要全设备覆盖时，另行正确配置TUN/VPN、DNS并实测。
- VPS到普通SOCKS5的认证通常没有TLS加密。客户端到VPS加密不代表所有链路都加密；优先使用可信供应商及HTTPS业务网站。

## 4. 管理

```bash
sbb                 # 统一管理菜单
sbb reality         # REALITY用户/连接码/端口/启停/日志
sbb path            # 原Path管理菜单
sbb status          # 两套服务状态
```

REALITY添加/删除用户、重置UUID或改端口会重启该方案，现有连接会断开并重连。更新通过管理菜单4重新选择对应方案，保留已有密钥与用户。旧版管理菜单请重新执行本文安装命令进入新版。启用失败尝试恢复旧文件；备份保存在root私有的 `/var/backups/sbb-reality.*`。

高位Path的证书、Token、安全组和Path格式详见 [Path方案说明](PATH-MODE.md)，安装时选择3，再选择1。原版80/443选择2，按本文步骤操作，不需要Token。

## 稳定、速度和兼容边界

REALITY采用固定版本官方Xray发布包，SHA256写在 `xray-release.json`，下载校验不通过就停止，不自动追随未经测试的新版本。默认关闭Mux。安装识别apt、dnf/yum、apk、zypper以及systemd/OpenRC；REALITY管理需要Python3.9及以上和amd64/arm64。软件源缺包、发行版太旧或目标不可达时会停止，并非所有系统已真机验证。

**高位端口不是提速或保证可达的方法。Xray自身也会提示REALITY非443端口在部分网络存在可达风险。** 此项目遵循“避开常用端口”的要求，不能同时保证任何国内网络都随时可连。实际速度由线路、带宽、CPU与住宅代理共同决定。

20台设备并不等于20条连接。测试包括原Path的32路并发，以及实际Xray/Mihomo核心的链式请求与住宅认证失败关闭；不等于20台真实设备长期高负载压力测试。iPhone操作和实际国内线路仍需用户验证。

技术依据：[Xray REALITY](https://github.com/XTLS/REALITY)、[Mihomo代理链](https://wiki.metacubex.one/config/proxies/dialer-proxy/)、[Certbot DNS验证](https://certbot-dns-cloudflare.readthedocs.io/en/stable/)。
