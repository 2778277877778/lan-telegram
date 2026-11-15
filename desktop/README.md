# lan-telegram 桌面壳（M3）

Telegram 式双栏界面：会话列表（未读徽标/预览/在线点）＋ 气泡对话（✓/✓✓ 回执、
引用回复、向前分页）＋ 输入栏。明暗主题跟随按钮，localStorage 记忆。

## 架构

```
WebView2 前端（desktop/ui，零框架 vanilla JS）
   │  CSP 全关，无任何出网能力；一切经 Tauri IPC
Rust 桥接层（src-tauri，命令见 main.rs）
   │  http://127.0.0.1:<local_port>（引擎的明文回环接入口）
Python 引擎（lantelegram.py serve）
   │  双向 TLS 1.3 + 指纹固定 —— 只在「引擎 ↔ 对端引擎」这条线上
对端机器
```

- 桥接层职责：端口/密钥从 `state.json`+`config.json` 自动发现（漂移自愈）、
  `/events` 长轮询 → `tg-events` 事件推送、全部阻塞 IO 走 `spawn_blocking`。
- 桌面壳不直连对端：TLS、证书、pins 全部复用引擎（单一事实源）。

## 开发运行

```powershell
# 1) 起引擎（数据目录与壳一致）
python ..\..\lantelegram.py serve --background

# 2) 构建壳（MSVC 工具链已在 rust-toolchain.toml 锁定；需 vcvars 环境）
cd desktop\src-tauri
cmd /c "`"...BuildTools\vcvars64.bat`" && cargo build"

# 3) 运行
target\debug\lan-telegram-desktop.exe
```

## 已知问题（都验证过根因）

1. **含空格的深层路径会让 WebView2 初始化失败**（`0x8000FFFF`，UDF 连目录都建不出来）。
   同一 exe 复制到短路径（如 `C:\Users\<you>\lt-run\`）立即正常。
   Program Files 常规安装不受影响（业界无数 Tauri 应用验证），但**开发时仓库若放在
   含空格/很深的目录，请用短路径副本跑壳**：把 exe 复制出去再启动即可。
2. **WebView2 崩溃孤儿锁**：宿主被强杀会遗留 `msedgewebview2.exe` 锁住用户数据目录，
   下次启动 `0x800700AA`。对策已内置：UDF 迁入 `LAN_TG_HOME\EBWebView`（环境变量
   `WEBVIEW2_USER_DATA_FOLDER`）、启动时按路径清孤儿、窗口创建失败重试 3 次。
3. **端口漂移双引擎**：主端口被占时引擎自动 +1 并回写配置。多实例测试务必先停干净
   再起（或显式 `serve --port`），否则会跑出两套引擎互相看不到。
4. GUI 内鼠标/键盘自动化（SetCursorPos/SendKeys 对高 DPI 下的 WebView2 子窗口）在
   Windows PowerShell 注入不可靠且会漏字到其他窗口——人工双击验证即可；
   等价操作链路已由 `tests/m3_bridge_check.py` 在桥接契约层全量覆盖（19 项）。
