# lan-telegram

**局域网聊天工具：没有服务器中转、没有账号、没有互联网依赖。消息点对点直连，只存在于你和对方的机器上。**

Telegram 式的对话体验——会话列表、气泡、送达/已读回执、离线补发、永久本地历史、图片与文件——
桌面端用 Rust + WebView2，协议引擎用纯 Python 标准库实现，可选 TLS 1.3 + 证书指纹固定加密。

它与 [lan-sync](https://github.com/2778277877778/lan-sync) 互传工具是两个独立应用：
lan-sync 是「传完就走」的一次性收发，lan-telegram 是「持续对话」的聊天。

> 最新发布 **v0.4.1**：Telegram 风格界面 · IP 一眼可见（含 VPN 多网卡）· 消息搜索 · 上传进度 ·
> 修掉「引擎起不来 / 取不到端口」的进程管理问题。
> Windows 免安装成品见 [Releases](https://github.com/2778277877778/lan-telegram/releases)。

## 现在能做什么

- **桌面客户端**：三栏 Telegram 式界面（会话列表 / 聊天区 / 资料面板），双击即用、无黑窗
- **一眼看到 IP**：本机地址常驻侧栏、可一键复制；对方 IP:端口 显示在聊天头和资料面板
- 文本、文件、图片互传（原件 + 缩略图），历史永久保存在本地 SQLite
- 已读回执（✓ / ✓✓）、消息幂等、离线队列自动补发（**含附件完整重传**）、历史分页
- **消息搜索**：跨会话搜文本与附件名，点结果直达对应会话
- **上传进度**：按块上报的真实百分比进度条
- **可选传输加密**：自签证书 + 指纹人工核对固定，只允许 TLS 1.3，错指纹握手即拒

## 为什么需要它

同办公室 / 宿舍两台电脑传聊天记录，走微信/QQ 要过第三方服务器，走网盘要手动搬。
lan-telegram 的威胁面只有一层局域网：

- **零中转**——消息点对点 HTTP(S) 直连，永不出局域网
- **防偷听**——可选 TLS 1.3：自签证书 + 首次配对人工核对指纹固定，不依赖任何 CA
- **防中间人**——未固定的对端、换过的证书、核对不符的指纹：全部在握手层拒绝，绝不回退明文
- **防伪造**——证书 CN 内嵌设备 ID，TLS 下声明的发信人必须与对端证书一致，否则 403
- **离线不断聊**——对方没开机时消息入队，上线后按 2/5/15/30/60 秒退避自动补发
- **历史在你手里**——SQLite 本地永久保存，不自动清理，不上传

## 环境要求

- **用成品**（Windows）：下载 Release 的 zip，解压双击，免装 Python
- **跑源码**：Python 3.8+（Windows / macOS / Linux）
  - 仅启用 TLS 时需要 `cryptography`：`pip install cryptography`
  - 不装也能跑：默认明文模式零第三方依赖

## 快速开始

### 方式 A：桌面客户端（推荐）

1. 两台机器各解压 Release 的 zip，双击 `lan-telegram-desktop.exe`
2. **看你这台的侧栏头像下方**——显示 `本机IP:端口`，例如 `26.50.11.175:32200`，点一下即复制
3. 把这个地址发给对方；对方在自己的「新对话」里填它
4. 双方对齐配对密钥（见下），否则互相 401

引擎没在跑时界面会显示引导卡，点「启动服务」或按 <kbd>Alt</kbd>+<kbd>S</kbd> 静默拉起，不会有黑框。

### 方式 B：命令行

两台机器各执行一遍，假设对端是 `26.50.11.175`：

```powershell
python lantelegram.py serve --background
python lantelegram.py info                      # 看设备名 / IP / 端口 / 密钥
python lantelegram.py pair --key <对方密钥>      # 两端密钥必须一致
python lantelegram.py send --target 26.50.11.175 --text "在吗"
python lantelegram.py history --with 26.50.11.175
```

对方离线时 `send` 会自动入队，等它上线补发——这就是「不断聊」。

### 连接不上时先查这三项

| 检查 | 说明 |
|---|---|
| 地址对不对 | 侧栏显示的是**本机在局域网/VPN 里的地址**；同一个 VPN 网段才连得上（如 Radmin/ZeroTier 给的 `26.x.x.x`） |
| 端口通不通 | 默认 `32200`；被占用时引擎会自动 +1，**以界面/`info` 显示的为准**，别用记下来的旧端口 |
| 密钥是否一致 | 不一致直接 401。`pair --key <对方密钥>` 对齐即可 |

### 可选：启用 TLS 1.3 加密

顺序很重要：**先互相固定，再各自开启**。

```powershell
python lantelegram.py cert                                         # ① 显示本机指纹（8 组）
python lantelegram.py trust --target 26.50.11.175:32200 --expect A1B2-C3D4-…  # ② 核对后固定对方
python lantelegram.py tls on                                       # ③ 切换并重启服务
```

指纹核对不符时 `trust` 会拒绝落盘——**这人工一步就是防中间人的全部**。
`tls off` 退回明文；`trust --list` 查看记录，对方换证书后 `trust --remove <旧指纹前缀>` 再重新配对。

## 命令参考

| 命令 | 作用 |
|---|---|
| `serve [--port N] [--background]` | 启动接收服务。端口被占自动 +1，以 `info` 为准 |
| `stop` / `status` / `info` | 停止 / 状态 / 本机信息（含 IP、端口、传输模式、指纹） |
| `send --target IP[:端口] --text "…" [--file 路径]` | 发消息或文件/图片；对方离线自动入队补发（含附件重传） |
| `search --query 关键词 [-q]` | 搜索本地消息（文本与附件名），显示 conv 与消息 id |
| `getfile --msg ID --out 路径` | 导出本机存过的附件（收到的或发出的） |
| `history [--with 设备] [--limit N] [--before TS]` | 会话列表 / 聊天记录（看完自动回执已读）；`--before` 向前翻页 |
| `contacts` | 已联系过的设备（IP / 端口 / 最近出现） |
| `key [--new]` / `pair --key K` | 查看 / 随机换 / 对齐应用层密钥 |
| `cert` | 查看/生成本机自签证书与指纹 |
| `trust --target IP[:端口] --expect 指纹` | 核对并固定对端证书；另有 `--pem`、`--list`、`--remove` |
| `tls on` / `tls off` / `tls` | 启用 / 关闭 TLS 1.3（on 需先固定至少一个对端） |

## 安全模型

**TLS 模式（推荐）**

- 协议版本强制 `min = max = TLS 1.3`（X25519，AES-256-GCM / ChaCha20-Poly1305，前向保密）
- 证书：Ed25519 自签，`BasicConstraints CA:TRUE` 自锚，CN = `lan-telegram/<设备ID>`，10 年
- 信任链：人工核对并固定的那份证书是**唯一信任锚**，不走系统 CA
- 双向认证：服务器同样要求客户端证书——未固定的一方连握手都过不去
- 身份绑定：`hello` / `messages` 声明的设备 ID 必须等于对端证书 CN（不符 403）

**明文模式（默认）**

- 应用层密钥 `X-Sync-Key` 只解决「发错人」，**不解决偷听**：同网段抓包可直接读
- 适合先试跑链路；正式使用请开启 TLS

**当前版本的边界**

- 密钥校验无速率限制与锁定（请用随机性足够的密钥，`key --new` 可换）
- 设备 IP 会变：靠 hello 自动刷新记录；跨子网/VPN 需手填地址
- 明文阶段对端可伪造 `from`（TLS 模式已封堵）

## 数据布局

```
%USERPROFILE%\.lan-telegram\        ← 环境变量 LAN_TG_HOME 可覆盖（macOS/Linux 为 ~/.lan-telegram）
├── config.json      设备 ID / 名称 / 端口 / 应用密钥 / TLS 开关
├── messages.db      SQLite：设备、会话、消息、回执、待发队列、事件流
├── state.json       运行状态（PID / 主端口 / 本机接入口端口）
├── server.log       服务日志
├── attachments/     文件与图片原件（按消息 UUID 命名）+ .thumb 缩略图
├── EBWebView/       桌面客户端的 WebView2 缓存（可删，会自动重建）
└── certs/
    ├── cert.pem     本机自签证书（把指纹念给对方）
    ├── key.pem      本机私钥（留在本机，永不外发）
    └── pins/        已固定的对端证书（pins.json 记录来源与时间）
```

聊天记录永久本地保存：不自动清理、不上传，删掉整个目录即清干净。

## 协议一览

HTTP 点对点，端点前缀 `/api/v2`：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/ping` | GET | 探活：版本 / 设备 ID / 端口 / **本机 IP 列表**（免密钥） |
| `/cert` | GET | 本机证书 PEM 与指纹（公开信息，配对阶段免密钥） |
| `/hello` | POST | 握手：交换设备 ID / 名称 / 端口 |
| `/messages` | POST | 投递文本消息，按 UUID 幂等去重 |
| `/upload` | POST | 投递文件/图片：原始字节流 + 元数据头；幂等，≤500 MB |
| `/file?msg=&thumb=` | GET | 取附件原件或缩略图 |
| `/search?q=&limit=` | GET | 本地消息检索（文本与附件名，通配符已转义） |
| `/send` `/conversations` `/contacts` `/read` | POST/GET | 界面层动作（见下「本机接入口」） |
| `/events?since=N` | GET | 长轮询增量事件（最多挂 25 秒），驱动界面实时刷新 |
| `/ack` | POST | 已读回执 |
| `/history` | GET | 分页拉历史 |

**本机接入口**：引擎除对等主端口外，固定另开一个 `127.0.0.1` 明文接入口（默认主端口+1，
实报于 `state.json.local_port`），只服务界面类端点、拒绝对等协议——桌面壳因此不需要
持有任何证书，TLS 模式下面板照常工作；引擎重启或端口漂移时，壳会按候选端口自动探活重新发现。

鉴权用 `X-Sync-Key` 头（或 `?key=`）。为什么用长轮询而不是 WebSocket、双时间戳防乱序等
取舍见 [docs/lan-telegram-design.md](docs/lan-telegram-design.md)。

## 测试与排障

验收脚本都是「同机起两个独立数据目录的实例」互测，可重复运行：

```powershell
# Windows GBK 控制台先切 UTF-8，否则脚本打印中文诊断会崩（与结果无关）
$env:PYTHONUTF8="1"

python tests/m1_check.py              # M1 12/12：收发/回执/幂等/分页/重启/离线入队
python tests/resend_check.py          # 离线队列自动补发
python tests/m2_check.py              # M2 13/13：指纹配对/TLS1.3 强制/明文与1.2拒绝/错指纹拒连
python tests/m3_bridge_check.py       # M3 19/19：GUI 端点契约/接入口隔离/TLS 下界面链路
python tests/m4_files_check.py        # M4 13/13：文件图片/幂等/缩略图/离线重传/上限
python tests/m5_search_progress_check.py  # M5 19/19：搜索转义与权限/上传进度事件
python tests/m6_runtime_check.py      # 运行时 15/15：僵尸 state/单实例/端口漂移/自动拉起/IP 列表
```

一次跑完：`python tests/run_all.py`（7 套，逐项打印汇总）。

常见问题：

| 现象 | 处理 |
|---|---|
| 发送报 401 | 两端密钥不一致：`pair --key <对方密钥>` |
| 一直连不上 | 先确认在**同一网段/VPN**、对方端口以它界面显示为准、防火墙放行 32200 |
| TLS 握手失败 / 提示证书不符 | 对方换过证书：`trust --remove <旧指纹前缀>` 后重新核对 `cert` 指纹再 trust |
| 一方开了 TLS 就都连不上 | 设计如此，**不会降级**。双方都要完成 trust + tls on |
| 端口不是 32200 | 被占用会自动 +1；桌面壳按候选端口自动探活，无需人工对齐 |
| 界面显示「引擎没有运行」 | 壳会自动尝试启动（最多 3 次）；都失败时看数据目录下 `server.log` 末尾的报错 |
| 取不到 IP / 端口显示 `?` | 说明引擎没在应答。先点「重试启动」；仍不行就 `lantelegram.exe stop` 后重开界面（v0.4.1 已能自动清僵尸状态与多实例） |
| 开了 VPN 后地址变了 | 正常。侧栏显示的是**当前各网卡地址**（VPN 虚拟网卡优先），点徽标复制的是首选地址；全部地址在右侧 ⓘ 面板里 |
| 搜不到消息 | 搜索只覆盖**本机**历史；换关键词或用附件名 |

## 路线图

- [x] **M1** SQLite 存储 + V2 协议 + 命令行 —— 12/12
- [x] **M2** TLS 1.3 + 自签证书 + 指纹固定 —— 13/13
- [x] **M3** 桌面客户端（Tauri 2 + WebView2，Rust 只做桥接，webview 零出网）—— 19/19
- [x] **M4** 文件与图片、缩略图、断线重传、上传进度 —— 13/13
- [x] **M5（部分）** 消息搜索 + 发送进度 —— 19/19；**群聊待做**
- [x] **M6** Windows 独立 exe —— Release 双 exe 分发（macOS/Linux 打包待做）

每个里程碑独立发版，都有可复跑的验收脚本。

## 与 lan-sync 的关系

[lan-sync](https://github.com/2778277877778/lan-sync) 是「传完就走」的局域网互传工具（文本+文件+GUI+网页端，v1.1.4 稳定）。
本项目源自它的传输骨架（HTTP 服务、密钥认证、打包链路），自 v0.1.0 起拆分为独立仓库、独立发版：
端口不同（32200 / 32100），数据目录不同（`.lan-telegram` / `.marvis-lan-sync`），互不影响，可同时运行。

## 许可证

[GNU General Public License v3.0](LICENSE)（GPL-3.0-only）。
你可以自由使用、修改、分发本软件；若分发修改后的版本，需以同一许可证开源。

## 贡献

欢迎 issue 与 PR。跑测试前先读「测试与排障」（Windows 需 `$env:PYTHONUTF8="1"`）；
提交前确保 `tests/` 下六个验收脚本全绿。
