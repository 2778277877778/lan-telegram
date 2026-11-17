// lan-telegram 桌面壳（M3/M4）：Tauri 2 + Rust 桥接层。
#![windows_subsystem = "windows"]

//
// 分工：所有网络与存储都留在既有 Python 引擎里（协议/加密/补发/去重的唯一事实源）。
// 本壳只做四件事——窗口、本机接入口发现（多候选端口自动探活）、把 /events
// 长轮询转成前端事件推送、以及引擎未运行时无黑窗拉起引擎。
// 双向 TLS 只存在于「引擎 ↔ 对端引擎」这条线；桌面壳不碰对端网络，
// webview 里因此永远不需要任何出网能力（CSP 全关）。

use base64::Engine as _;
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State};

#[derive(Clone)]
struct Bridge {
    port: u16,
    key: String,
}

struct App {
    home: PathBuf,
    bridge: Mutex<Option<Bridge>>,
}

impl App {
    fn data_home() -> PathBuf {
        if let Ok(v) = std::env::var("LAN_TG_HOME") {
            if !v.is_empty() {
                return PathBuf::from(v);
            }
        }
        let base = std::env::var("USERPROFILE")
            .or_else(|_| std::env::var("HOME"))
            .unwrap_or_else(|_| ".".into());
        PathBuf::from(base).join(".lan-telegram")
    }

    fn read_json(&self, rel: &str) -> Value {
        let p = self.home.join(rel);
        std::fs::read_to_string(p)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or(Value::Null)
    }

    /// 自适应端口发现：按候选顺序探活 /api/v2/ping，取第一个应答者。
    /// 候选 = state.json 实报 local_port → state port+1 → config port+1..+6。
    /// 引擎漂端口/重启换口/多实例共存都能自愈，不再依赖单一约定。
    fn resolve(&self) -> Result<Bridge, String> {
        let cfg = self.read_json("config.json");
        let key = cfg
            .get("key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if key.is_empty() {
            return Err("本机还没初始化：先运行 python lantelegram.py serve --background".into());
        }
        let state = self.read_json("state.json");
        let mut cands: Vec<u16> = Vec::new();
        let push = |v: Option<u64>, c: &mut Vec<u16>| {
            if let Some(p) = v {
                let p = p as u16;
                if !c.contains(&p) {
                    c.push(p);
                }
            }
        };
        push(state.get("local_port").and_then(Value::as_u64), &mut cands);
        push(
            state.get("port").and_then(Value::as_u64).map(|p| p + 1),
            &mut cands,
        );
        if let Some(cp) = cfg.get("port").and_then(Value::as_u64) {
            for k in 1..=6u64 {
                push(Some(cp + k), &mut cands);
            }
        }
        for &p in &cands {
            if Self::probe(p, &key) {
                return Ok(Bridge { port: p, key });
            }
        }
        Err("没找到在跑的本机引擎接口（serve 未运行？可点「启动服务」）".into())
    }

    fn probe(port: u16, key: &str) -> bool {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_millis(800))
            .no_proxy()
            .build();
        match client {
            Ok(c) => match c
                .get(format!("http://127.0.0.1:{}/api/v2/ping", port))
                .header("X-Sync-Key", key)
                .send()
            {
                Ok(r) => r.status().as_u16() == 200,
                Err(_) => false,
            },
            Err(_) => false,
        }
    }

    fn bridge(&self) -> Result<Bridge, String> {
        let mut guard = self.bridge.lock().map_err(|_| "锁中毒".to_string())?;
        if let Some(b) = guard.clone() {
            // 用前轻探一次缓存端口，失败即作废重选（引擎可能刚重启换口）
            if Self::probe(b.port, &b.key) {
                return Ok(b);
            }
        }
        let b = self.resolve()?;
        *guard = Some(b.clone());
        Ok(b)
    }

    fn force_refresh(&self) -> Result<Bridge, String> {
        if let Ok(mut g) = self.bridge.lock() {
            *g = None;
        }
        self.resolve()
    }

    fn call(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<Value, String> {
        let b = match self.bridge() {
            Ok(b) => b,
            Err(e) => return Err(e),
        };
        let result = self.raw_call(&b, method, path, body, timeout);
        if result.is_err() {
            if let Ok(mut g) = self.bridge.lock() {
                *g = None; // 引擎可能刚重启/漂口：下次调用重新发现
            }
        }
        result
    }

    fn raw_call(
        &self,
        b: &Bridge,
        method: &str,
        path: &str,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<Value, String> {
        let url = format!("http://127.0.0.1:{}/api/v2/{}", b.port, path);
        let client = reqwest::blocking::Client::builder()
            .timeout(timeout)
            .no_proxy()
            .build()
            .map_err(|e| e.to_string())?;
        let mut req = match method {
            "POST" => client.post(&url),
            _ => client.get(&url),
        };
        req = req.header("X-Sync-Key", &b.key);
        if let Some(v) = body {
            req = req.json(v);
        }
        let resp = req
            .send()
            .map_err(|e| format!("连不上引擎（{e}）——可能没 serve，或刚换过端口"));
        match resp {
            Ok(r) => {
                let status = r.status().as_u16();
                let mut v = r.json::<Value>().unwrap_or(json!({"ok": false, "error": "返回不可解析"}));
                if status >= 400 {
                    if let Some(o) = v.as_object_mut() {
                        o.insert("__http_status".into(), json!(status));
                    }
                }
                Ok(v)
            }
            Err(e) => Err(e),
        }
    }

    fn raw_bytes(
        &self,
        path: &str,
        timeout: Duration,
    ) -> Result<Vec<u8>, String> {
        let b = self.bridge()?;
        let url = format!("http://127.0.0.1:{}/api/v2/{}", b.port, path);
        let client = reqwest::blocking::Client::builder()
            .timeout(timeout)
            .no_proxy()
            .build()
            .map_err(|e| e.to_string())?;
        let resp = client
            .get(&url)
            .header("X-Sync-Key", &b.key)
            .send()
            .map_err(|e| e.to_string())?;
        if resp.status().as_u16() != 200 {
            return Err(format!("HTTP {}", resp.status().as_u16()));
        }
        resp.bytes().map(|x| x.to_vec()).map_err(|e| e.to_string())
    }
}

/// 在 spawn_blocking 线程里安全取状态并执行；任务 panic 也转成结构化错误。
async fn off<F>(app: AppHandle, f: F) -> Value
where
    F: FnOnce(&State<'_, App>) -> Result<Value, String> + Send + 'static,
{
    let res = tauri::async_runtime::spawn_blocking(move || {
        let st: State<App> = app.state();
        f(&st)
    })
    .await;
    match res {
        Ok(Ok(v)) => v,
        Ok(Err(error)) => json!({"ok": false, "error": error}),
        Err(_) => json!({"ok": false, "error": "桥接任务异常，请重启桌面壳"}),
    }
}

#[tauri::command]
async fn engine_state(app: AppHandle) -> Value {
    off(app, |st| {
        let cfg = st.read_json("config.json");
        let b = st.bridge();
        let ping = match &b {
            Ok(b) => st
                .raw_call(b, "GET", "ping", None, Duration::from_secs(3))
                .unwrap_or(Value::Null),
            Err(_) => Value::Null,
        };
        Ok(json!({
            "ok": true,
            "running": ping != serde_json::Value::Null,
            "tls": cfg.get("tls").and_then(Value::as_bool).unwrap_or(false),
            "device_name": cfg.get("device_name").cloned().unwrap_or(Value::Null),
            "device_id": cfg.get("device_id").cloned().unwrap_or(Value::Null),
            "local_port": b.as_ref().map(|x| json!(x.port)).unwrap_or(Value::Null),
            "ping": ping,
        }))
    })
    .await
}

/// 引擎没跑时由界面一键拉起：无黑窗启动，然后等探活成功。
#[tauri::command]
async fn ensure_server(app: AppHandle) -> Value {
    off(app, |st| {
        if st.bridge().is_ok() {
            return Ok(json!({"ok": true, "running": true, "started": false}));
        }
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."));
        let mut launched = false;
        // 1) 发布形态：桌面壳同目录（或 engine/ 子目录）的 lantelegram.exe
        for cand in [
            exe_dir.join("lantelegram.exe"),
            exe_dir.join("engine").join("lantelegram.exe"),
        ] {
            if cand.exists() {
                let mut cmd = std::process::Command::new(&cand);
                cmd.args(["serve", "--background"]);
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    cmd.creation_flags(0x0800_0000);
                }
                let _ = cmd.stdout(std::process::Stdio::null())
                    .stderr(std::process::Stdio::null())
                    .spawn();
                launched = true;
                break;
            }
        }
        // 2) 开发形态：从 exe 目录向上找 lantelegram.py，用 pythonw/python 起（隐藏窗口）
        if !launched {
            let mut dir = exe_dir.clone();
            for _ in 0..6 {
                let script = dir.join("lantelegram.py");
                if script.exists() {
                    let pyw = which_pythonw();
                    let mut cmd = std::process::Command::new(&pyw);
                    cmd.arg(&script).args(["serve", "--background"]);
                    cmd.env("LAN_TG_HOME", &st.home);
                    #[cfg(windows)]
                    {
                        use std::os::windows::process::CommandExt;
                        cmd.creation_flags(0x0800_0000);
                    }
                    let _ = cmd.stdout(std::process::Stdio::null())
                        .stderr(std::process::Stdio::null())
                        .spawn();
                    launched = true;
                    break;
                }
                dir = match dir.parent() {
                    Some(p) => p.to_path_buf(),
                    None => break,
                };
            }
        }
        if !launched {
            return Ok(json!({"ok": false, "error": "找不到 lantelegram.exe 或 lantelegram.py"}));
        }
        for _ in 0..40 {
            thread::sleep(Duration::from_millis(400));
            if let Ok(b) = st.force_refresh() {
                return Ok(json!({"ok": true, "running": true, "started": true, "port": b.port}));
            }
        }
        Ok(json!({"ok": false, "error": "已尝试启动，16 秒内引擎未应答，请看 server.log"}))
    })
    .await
}

#[cfg(windows)]
fn which_pythonw() -> String {
    // pythonw 无控制台；找不到就退回 python（spawn 已加 CREATE_NO_WINDOW）
    "pythonw".to_string()
}
#[cfg(not(windows))]
fn which_pythonw() -> String {
    "python3".to_string()
}

/// 极简百分号编码：只保留 unreserved 字符，其余按 UTF-8 字节转义。
/// 查询词可能含中文、空格、% 等，必须编码后再拼进查询串。
fn urlenc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.as_bytes() {
        match *b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*b as char)
            }
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

#[tauri::command]
async fn search_messages(app: AppHandle, q: String, limit: Option<u64>) -> Value {
    off(app, move |st| {
        let n = limit.unwrap_or(50);
        st.call(
            "GET",
            &format!("search?q={}&limit={}", urlenc(&q), n),
            None,
            Duration::from_secs(8),
        )
    })
    .await
}

/// 会话列表（conv 空）或某会话最近消息。
#[tauri::command]
async fn read_view(app: AppHandle, conv: Option<String>, limit: Option<u64>) -> Value {
    off(app, move |st| {
        let conv = conv.unwrap_or_default();
        if conv.is_empty() {
            return st.call("GET", "conversations", None, Duration::from_secs(5));
        }
        let n = limit.unwrap_or(50);
        st.call(
            "GET",
            &format!("history?conv={}&limit={}", conv, n),
            None,
            Duration::from_secs(8),
        )
    })
    .await
}

/// 向前翻页：before 传当前最旧一条的 ts。
#[tauri::command]
async fn history_before(app: AppHandle, conv: String, before: i64, limit: Option<u64>) -> Value {
    off(app, move |st| {
        let n = limit.unwrap_or(50);
        st.call(
            "GET",
            &format!("history?conv={}&before={}&limit={}", conv, before, n),
            None,
            Duration::from_secs(8),
        )
    })
    .await
}

#[tauri::command]
async fn send_text(app: AppHandle, to: String, text: String, reply_to: Option<String>) -> Value {
    off(app, move |st| {
        let mut body = json!({"to": to, "text": text});
        if let Some(r) = reply_to {
            body["reply_to"] = json!(r);
        }
        st.call("POST", "send", Some(&body), Duration::from_secs(25))
    })
    .await
}

/// 发文件/图片：path 本机绝对路径；thumb_b64 由前端 canvas 生成的 PNG 缩略图。
/// client_ref 由前端生成，会原样出现在 progress 事件里，用于把进度绑到对应气泡。
#[tauri::command]
async fn send_file(
    app: AppHandle,
    to: String,
    path: String,
    thumb_b64: Option<String>,
    reply_to: Option<String>,
    client_ref: Option<String>,
) -> Value {
    off(app, move |st| {
        let mut body = json!({"to": to, "path": path});
        if let Some(t) = thumb_b64 {
            body["thumb_b64"] = json!(t);
        }
        if let Some(r) = reply_to {
            body["reply_to"] = json!(r);
        }
        if let Some(c) = client_ref {
            body["client_ref"] = json!(c);
        }
        // 大文件在引擎里同步流式传给对端，超时放宽到 5 分钟
        st.call("POST", "send", Some(&body), Duration::from_secs(300))
    })
    .await
}

/// 查看即已读：回执由服务端发，桌面壳不自行判定。
#[tauri::command]
async fn mark_read(app: AppHandle, conv: Option<String>) -> Value {
    off(app, move |st| {
        let body = match conv {
            Some(c) if !c.is_empty() => json!({"conv": c}),
            _ => json!({}),
        };
        st.call("POST", "read", Some(&body), Duration::from_secs(10))
    })
    .await
}

#[tauri::command]
async fn contact_list(app: AppHandle) -> Value {
    off(app, |st| st.call("GET", "contacts", None, Duration::from_secs(5)))
    .await
}

/// 取附件字节（缩略图或原件），返回 base64；图片渲染用 dataURL。
#[tauri::command]
async fn get_blob(app: AppHandle, msg: String, thumb: bool) -> Value {
    off(app, move |st| {
        let path = format!(
            "file?msg={}&thumb={}",
            msg,
            if thumb { "1" } else { "0" }
        );
        let t = if thumb {
            Duration::from_secs(20)
        } else {
            Duration::from_secs(300)
        };
        match st.raw_bytes(&path, t) {
            Ok(v) => {
                let mime_guess = if thumb { "image/png" } else { "application/octet-stream" };
                Ok(json!({
                    "ok": true,
                    "b64": base64::engine::general_purpose::STANDARD.encode(&v),
                    "size": v.len(),
                    "mime": mime_guess,
                }))
            }
            Err(e) => Ok(json!({"ok": false, "error": e})),
        }
    })
    .await
}

/// 另存附件到用户选定路径（前端经 dialog.save 拿到路径再调这里）。
#[tauri::command]
async fn save_blob(app: AppHandle, msg: String, dest: String, thumb: bool) -> Value {
    off(app, move |st| {
        let path = format!(
            "file?msg={}&thumb={}",
            msg,
            if thumb { "1" } else { "0" }
        );
        match st.raw_bytes(&path, Duration::from_secs(300)) {
            Ok(v) => {
                let p = PathBuf::from(&dest);
                match std::fs::write(&p, &v) {
                    Ok(_) => Ok(json!({"ok": true, "path": dest, "size": v.len()})),
                    Err(e) => Ok(json!({"ok": false, "error": e.to_string()})),
                }
            }
            Err(e) => Ok(json!({"ok": false, "error": e})),
        }
    })
    .await
}

/// 用系统默认程序打开附件（缓存到临时目录再 start，无黑窗）。
#[tauri::command]
async fn open_blob(app: AppHandle, msg: String, name: String) -> Value {
    off(app, move |st| match st.raw_bytes(&format!("file?msg={}", msg), Duration::from_secs(300)) {
        Ok(v) => {
            let safe: String = name
                .chars()
                .map(|c| match c {
                    '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => '_',
                    _ if (c as u32) < 0x20 => '_',
                    _ => c,
                })
                .collect();
            let dir = std::env::temp_dir().join("lt-view");
            let _ = std::fs::create_dir_all(&dir);
            let file = if safe.is_empty() {
                dir.join(format!("{}.bin", msg))
            } else {
                dir.join(safe)
            };
            match std::fs::write(&file, &v) {
                Ok(_) => {
                    #[cfg(windows)]
                    {
                        use std::os::windows::process::CommandExt;
                        let _ = std::process::Command::new("cmd")
                            .args(["/C", "start", "", &file.to_string_lossy().to_string()])
                            .creation_flags(0x0800_0000)
                            .spawn();
                    }
                    #[cfg(not(windows))]
                    {
                        let _ = std::process::Command::new("xdg-open")
                            .arg(&file)
                            .spawn();
                    }
                    Ok(json!({"ok": true, "path": file.to_string_lossy()}))
                }
                Err(e) => Ok(json!({"ok": false, "error": e.to_string()})),
            }
        }
        Err(e) => Ok(json!({"ok": false, "error": e})),
    })
    .await
}

/// 供前端为图片生成缩略图预览：读本机文件（≤8MB）返回 dataURL。
#[tauri::command]
async fn read_preview(app: AppHandle, path: String) -> Value {
    off(app, move |_st| {
        let p = PathBuf::from(&path);
        let meta = std::fs::metadata(&p).map_err(|e| e.to_string())?;
        if meta.len() > 8 * 1024 * 1024 {
            return Ok(json!({"ok": true, "preview": null, "big": true}));
        }
        let bytes = std::fs::read(&p).map_err(|e| e.to_string())?;
        let ext = p
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase();
        let mime = match ext.as_str() {
            "png" => "image/png",
            "jpg" | "jpeg" => "image/jpeg",
            "gif" => "image/gif",
            "webp" => "image/webp",
            "bmp" => "image/bmp",
            _ => "application/octet-stream",
        };
        if !mime.starts_with("image/") {
            return Ok(json!({"ok": true, "preview": null}));
        }
        Ok(json!({
            "ok": true,
            "preview": format!("data:{};base64,{}", mime,
                base64::engine::general_purpose::STANDARD.encode(&bytes)),
        }))
    })
    .await
}

/// 事件泵：先把存量静默排空（游标对齐到最新），再进入 20 秒长轮询。
/// 断连时退避并重新发现端口（引擎可能刚重启）。
fn event_pump(app: AppHandle) {
    let mut since: i64 = 0;
    // 静默排空存量
    loop {
        let next = {
            let st: State<App> = app.state();
            match st.call(
                "GET",
                &format!("events?since={}&timeout=0", since),
                None,
                Duration::from_secs(5),
            ) {
                Ok(v) => {
                    let got = v
                        .get("events")
                        .and_then(Value::as_array)
                        .map(|a| a.len())
                        .unwrap_or(0);
                    if let Some(cur) = v.get("cursor").and_then(Value::as_i64) {
                        since = cur;
                    }
                    if got == 0 {
                        true
                    } else {
                        continue;
                    }
                }
                Err(_) => {
                    thread::sleep(Duration::from_secs(3));
                    false
                }
            }
        };
        if next {
            break;
        }
    }
    // 增量推送
    loop {
        let ok = {
            let st: State<App> = app.state();
            let path = format!("events?since={}&timeout=20", since);
            match st.call("GET", &path, None, Duration::from_secs(26)) {
                Ok(v) => {
                    if let Some(cur) = v.get("cursor").and_then(Value::as_i64) {
                        since = cur;
                    }
                    let events = v.get("events").cloned().unwrap_or(json!([]));
                    if events.as_array().map(|a| !a.is_empty()).unwrap_or(false) {
                        let _ = app.emit("tg-events", events);
                    }
                    true
                }
                Err(_) => false,
            }
        };
        if !ok {
            thread::sleep(Duration::from_secs(3));
        }
    }
}

fn main() {
    let app = App {
        home: App::data_home(),
        bridge: Mutex::new(None),
    };

    // WebView2 用户数据目录：默认在 exe 旁（target/debug/xxx.exe.WebView2）。
    // 宿主崩溃遗留的 msedgewebview2 孤儿会锁住目录，后续实例以 0x800700AA/0x8000FFFF
    // 失败。对策：UDF 迁入数据目录 + 启动清本应用孤儿 + 窗口创建失败重试。
    let udf = app.home.join("EBWebView");
    let _ = std::fs::create_dir_all(&udf);
    std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &udf);
    let stem = std::env::current_exe()
        .ok()
        .and_then(|p| p.file_stem().map(|s| s.to_string_lossy().into_owned()))
        .unwrap_or_else(|| "lan-telegram-desktop".into());
    let udf_str = udf.to_string_lossy().into_owned();
    kill_orphan_webviews(&udf_str, &stem);

    let udf_for_setup = udf_str.clone();
    let stem_for_setup = stem.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(app)
        .invoke_handler(tauri::generate_handler![
            engine_state,
            ensure_server,
            read_view,
            history_before,
            send_text,
            send_file,
            mark_read,
            contact_list,
            get_blob,
            save_blob,
            open_blob,
            read_preview,
            search_messages
        ])
        .setup(move |a| {
            // 窗口自建模带重试（config 里 windows 置空，避免内部一次性创建）
            let mut last_err = String::new();
            for _attempt in 0..3 {
                let built = tauri::WebviewWindowBuilder::new(
                    a,
                    "main",
                    tauri::WebviewUrl::App("index.html".into()),
                )
                .title("lan-telegram")
                .inner_size(1020.0, 680.0)
                .min_inner_size(660.0, 440.0)
                .center()
                .build();
                match built {
                    Ok(_w) => {
                        last_err.clear();
                        break;
                    }
                    Err(e) => {
                        last_err = e.to_string();
                        kill_orphan_webviews(&udf_for_setup, &stem_for_setup);
                        std::thread::sleep(std::time::Duration::from_millis(1500));
                    }
                }
            }
            if !last_err.is_empty() {
                eprintln!("窗口创建失败（已重试 3 次）：{last_err}");
                return Err(last_err.into());
            }
            let handle = a.app_handle().clone();
            thread::spawn(move || event_pump(handle));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("lan-telegram 桌面壳启动失败");
}

/// 清掉锁着本应用 WebView2 用户数据目录的孤儿浏览器进程（宿主崩溃遗留）。
/// 只按路径匹配（当前 EBWebView + 旧版 exe 旁 UDF），不动其他应用的 WebView2。
#[cfg(windows)]
fn kill_orphan_webviews(udf: &str, stem: &str) {
    use std::os::windows::process::CommandExt;
    let legacy = format!("{}.exe.WebView2", stem);
    let esc = |s: &str| s.replace('\'', "\"'\"'\"\"'");
    let ps = format!(
        "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | \
         Where-Object {{ $_.CommandLine -like '*{}*' -or $_.CommandLine -like '*{}*' }} | \
         ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }}",
        esc(udf),
        esc(&legacy)
    );
    let _ = std::process::Command::new("powershell")
        .args(["-NoProfile", "-Command", &ps])
        .creation_flags(0x0800_0000)
        .output();
}

#[cfg(not(windows))]
fn kill_orphan_webviews(_udf: &str, _stem: &str) {}
