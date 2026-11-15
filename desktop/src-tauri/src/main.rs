// lan-telegram 桌面壳（M3）：Tauri 2 + Rust 桥接层。
#![windows_subsystem = "windows"]

//
// 分工：所有网络与存储都留在既有 Python 引擎里（协议/加密/补发/去重的唯一事实源）。
// 本壳只做三件事——窗口、把引擎的本机回环接入口（明文 127.0.0.1，仅 GUI 端点）
// 包装成类型化命令、把 /events 长轮询转成前端事件推送。
// 双向 TLS 只存在于「引擎 ↔ 对端引擎」那条线；桌面壳不碰对端网络，
// webview 里因此永远不需要任何出网能力（CSP 全关）。
//
// 阻塞式 HTTP 一律进 spawn_blocking：同步命令会占住 IPC 线程，拖慢事件推送。

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

    /// 接入口发现：state.json 实报优先，退回「config 主端口 + 1」约定；密钥取 config。
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
        let port = state
            .get("local_port")
            .and_then(Value::as_u64)
            .or_else(|| cfg.get("port").and_then(Value::as_u64).map(|p| p + 1))
            .ok_or_else(|| "读不到引擎端口，请确认 serve 在运行".to_string())?;
        Ok(Bridge {
            port: port as u16,
            key,
        })
    }

    fn bridge(&self) -> Result<Bridge, String> {
        let mut guard = self.bridge.lock().map_err(|_| "锁中毒".to_string())?;
        if let Some(b) = guard.clone() {
            return Ok(b);
        }
        let b = self.resolve()?;
        *guard = Some(b.clone());
        Ok(b)
    }

    fn forget_bridge(&self) {
        if let Ok(mut g) = self.bridge.lock() {
            *g = None;
        }
    }

    fn call(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<Value, String> {
        let b = self.bridge()?;
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
        let result = req
            .send()
            .map_err(|e| format!("连不上引擎（{e}）——可能没 serve，或刚换过端口"))
            .and_then(|r| r.json::<Value>().map_err(|e| format!("引擎返回不可解析：{e}")));
        if result.is_err() {
            self.forget_bridge(); // 端口可能漂了：下次调用重新发现
        }
        result
    }
}

fn wrap(res: Result<Value, String>) -> Value {
    match res {
        Ok(v) => v,
        Err(error) => json!({"ok": false, "error": error}),
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
        Ok(inner) => wrap(inner),
        Err(_) => json!({"ok": false, "error": "桥接任务异常，请重启桌面壳"}),
    }
}

#[tauri::command]
async fn engine_state(app: AppHandle) -> Value {
    off(app, |st| {
        let ping = st.call("GET", "ping", None, Duration::from_secs(3));
        let cfg = st.read_json("config.json");
        let port = st.bridge().map(|b| b.port);
        Ok(json!({
            "ok": true,
            "tls": cfg.get("tls").and_then(Value::as_bool).unwrap_or(false),
            "device_name": cfg.get("device_name").cloned().unwrap_or(Value::Null),
            "device_id": cfg.get("device_id").cloned().unwrap_or(Value::Null),
            "local_port": port.unwrap_or(0),
            "ping": ping.unwrap_or(Value::Null),
        }))
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
    off(app, |st| {
        st.call("GET", "contacts", None, Duration::from_secs(5))
    })
    .await
}

/// 清掉锁着「旧版 exe 旁 UDF」的孤儿 msedgewebview2 进程（宿主崩溃遗留）。
/// 只匹配本 exe 文件名的 .WebView2 路径，不动其他应用的 WebView2。
#[cfg(windows)]
fn kill_orphan_webviews() {
    use std::os::windows::process::CommandExt;
    let stem = std::env::current_exe()
        .ok()
        .and_then(|p| p.file_stem().map(|s| s.to_string_lossy().into_owned()))
        .unwrap_or_default();
    if stem.is_empty() {
        return;
    }
    let pattern = format!("{}.exe.WebView2", stem);
    let ps = format!(
        "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | \
         Where-Object {{ $_.CommandLine -like '*{}*' }} | \
         ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }}",
        pattern
    );
    let _ = std::process::Command::new("powershell")
        .args(["-NoProfile", "-Command", &ps])
        .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
        .output();
}

#[cfg(not(windows))]
fn kill_orphan_webviews() {}

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
                    st.forget_bridge();
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
                Err(_) => {
                    st.forget_bridge();
                    false
                }
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
    // 崩溃遗留的 msedgewebview2 会锁住该目录，后续实例以 0x800700AA/拒绝访问 失败。
    // 1) 把 UDF 挪进数据目录（同时解决 Program Files 不可写）；2) 清掉只锁着旧 UDF 的孤儿进程。
    let udf = app.home.join("EBWebView");
    let _ = std::fs::create_dir_all(&udf);
    std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &udf);
    kill_orphan_webviews();

    tauri::Builder::default()
        .manage(app)
        .invoke_handler(tauri::generate_handler![
            engine_state,
            read_view,
            history_before,
            send_text,
            mark_read,
            contact_list
        ])
        .setup(|a| {
            let handle = a.app_handle().clone();
            thread::spawn(move || event_pump(handle));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("lan-telegram 桌面壳启动失败");
}
