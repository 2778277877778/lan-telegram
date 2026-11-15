# -*- coding: utf-8 -*-
"""M3 验收（服务端契约）：GUI 端点全链路 + TLS 模式下接入口隔离。

角色：A/B 两个实例；GUI（桌面壳/网页）只会话本机回环接入口，局域网投递由引擎负责。
本脚本验证 Rust/网页两个前端共同依赖的 HTTP 契约与行为语义。
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "lantelegram.py"
BASE = Path(os.environ.get("TEMP", ".")) / "lt-m3"
KEY = "m3key0123"
PORT = {"A": 32310, "B": 32320}      # 主端口；本机接入口 = 主端口 + 1

results = []
procs = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def home(name):
    return BASE / name


def env(name):
    return dict(os.environ, LAN_TG_HOME=str(home(name)))


def run(name, *args):
    return subprocess.run([sys.executable, str(APP), *args], env=env(name),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def start(name, tls=False):
    h = home(name)
    h.mkdir(parents=True, exist_ok=True)
    cfgf = h / "config.json"
    cfg = {"device_id": f"dev{name}{'0' * 28}"[:32], "device_name": name,
           "port": PORT[name], "key": KEY}
    if cfgf.exists():
        cfg.update(json.loads(cfgf.read_text(encoding="utf-8")))
    cfg["tls"] = tls
    cfgf.write_text(json.dumps(cfg), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(APP), "serve"], env=env(name),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


def http(port, method, path, body=None, key=KEY, timeout=8):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    req.add_header("X-Sync-Key", key)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def wait_up(port, path="/api/v2/ping", timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_tls(port, name, timeout=20):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    pins = home(name) / "certs" / "pins"
    ctx.load_verify_locations(cadata="".join(f.read_text(encoding="utf-8")
                                             for f in pins.glob("*.pem")))
    ctx.load_cert_chain(str(home(name) / "certs" / "cert.pem"),
                        str(home(name) / "certs" / "key.pem"))
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                with ctx.wrap_socket(s, server_hostname="127.0.0.1") as t:
                    if t.version() == "TLSv1.3":
                        return True
        except Exception:
            time.sleep(0.3)
    return False


L = {n: PORT[n] + 1 for n in ("A", "B")}     # 本机 GUI 接入口

shutil.rmtree(BASE, ignore_errors=True)
try:
    pA = start("A")
    pB = start("B")
    print("=== 启动 ===")
    check("A/B 主端口与接入口就绪",
          all(wait_up(x) for x in (PORT["A"], PORT["B"], L["A"], L["B"])))

    print("=== GUI 契约（明文） ===")
    r = http(L["A"], "POST", "/api/v2/send", {"to": f"127.0.0.1:{PORT['B']}",
                                              "text": "GUI第一句"})
    a_conv = r.get("message", {}).get("conv", "")
    check("A 经接入口 send → 投递成功", r.get("ok") and r.get("state") in ("delivered", "sent"),
          str(r.get("state")))
    r = http(L["B"], "GET", "/api/v2/conversations")
    c = (r.get("conversations") or [{}])[0]
    check("B 会话列表含未读与预览", r.get("ok") and c.get("unread") == 1
          and "GUI第一句" in (c.get("preview") or ""), f"unread={c.get('unread')}")
    b_conv = c.get("id") or ""
    r = http(L["B"], "GET", f"/api/v2/history?conv={b_conv}")
    items = r.get("items") or []
    check("B history 结构化 body", items and isinstance(items[-1].get("body"), dict)
          and items[-1]["body"].get("text") == "GUI第一句")
    r = http(L["B"], "POST", "/api/v2/read", {"conv": b_conv})
    check("B read 触发回执", r.get("ok") and r.get("acked") == 1)
    time.sleep(0.8)
    r = http(L["A"], "GET", f"/api/v2/history?conv={a_conv}")
    m = (r.get("items") or [{}])[-1]
    check("A 侧消息变已读", m.get("state") == "read", m.get("state"))
    r = http(L["A"], "GET", "/api/v2/events?since=0")
    types = [e.get("type") for e in (r.get("events") or [])]
    check("A 事件流含 receipt", "receipt" in types, ",".join(sorted(set(types))))
    r = http(L["B"], "POST", "/api/v2/hello", {"device_id": "x"})
    check("接入口拒绝对等端点", r.get("error") == "not found")
    r = http(L["A"], "POST", "/api/v2/send", {"to": f"127.0.0.1:{PORT['B']}", "text": "坏密钥"},
             key="WRONG")
    check("接入口密钥校验 401", "密钥不匹配" in (r.get("error") or ""))
    r = http(L["A"], "POST", "/api/v2/send", {"to": "127.0.0.1:59999", "text": "未知且离线"})
    # 同机环境下 127.0.0.1 是已知聊天过的 IP：send 到该 IP 的任意端口均按「聊过→入队」
    # 原语处理（与 M1 CLI 语义一致）；此处断言该原语正确，而非断言特定端口失败
    check("同 IP 未知端口按已知设备入队（M1 原语）",
          bool(r.get("ok")) and bool(r.get("queued")))
    r = http(L["A"], "POST", "/api/v2/send", {"to": "192.0.2.123:60099", "text": "陌生设备"},
             timeout=20)
    check("陌生 IP 且离线 → 拒绝不入队", not r.get("ok"))

    print("=== TLS 模式 ===")
    run("A", "cert"); run("B", "cert")
    pemA = http(PORT["A"], "GET", "/api/v2/cert").get("cert_pem") or ""
    pemB = http(PORT["B"], "GET", "/api/v2/cert").get("cert_pem") or ""
    # 走 HTTP 拿到的是字符串 PEM：落盘后用 CLI trust 走正式路径
    (BASE / "a.pem").write_text(pemA, encoding="utf-8")
    (BASE / "b.pem").write_text(pemB, encoding="utf-8")
    check("明文阶段 /cert 可拉证书（配对前置）",
          pemA.startswith("-----BEGIN CERTIFICATE") and pemB.startswith("-----BEGIN CERTIFICATE"))
    fpA = run("A", "cert").stdout.split("完整指纹")[-1].split("\n")[0].split(":")[-1].strip()
    fpB = run("B", "cert").stdout.split("完整指纹")[-1].split("\n")[0].split(":")[-1].strip()
    ok_a = "已固定对端证书" in run("A", "trust", "--pem", str(BASE / "b.pem"),
                                   "--expect", fpB[:32]).stdout
    ok_b = "已固定对端证书" in run("B", "trust", "--pem", str(BASE / "a.pem"),
                                   "--expect", fpA[:32]).stdout
    check("双向 trust（--pem 离线导入）", ok_a and ok_b)

    pA.terminate(); pA.wait(timeout=10)
    pB.terminate(); pB.wait(timeout=10)
    time.sleep(0.5)
    pA = start("A", tls=True); procs.append(pA)
    pB = start("B", tls=True); procs.append(pB)
    check("TLS 服务启动（主端口 1.3 握手）",
          wait_tls(PORT["A"], "B") and wait_tls(PORT["B"], "A"))
    check("接入口保持明文可用",
          all(wait_up(x) for x in (L["A"], L["B"]))
          and http(L["A"], "GET", "/api/v2/conversations").get("ok"))

    r = http(L["A"], "POST", "/api/v2/send", {"to": "B", "text": "TLS经接入口"})
    check("TLS 下 GUI send 全通（引擎走双向 TLS 投递）",
          r.get("ok") and r.get("state") in ("delivered", "sent"), str(r.get("state", r)))
    rb_conv = ""
    for cv in (http(L["B"], "GET", "/api/v2/conversations").get("conversations") or []):
        if "TLS经接入口" in (cv.get("preview") or ""):
            rb_conv = cv["id"]
            break
    r = http(L["B"], "POST", "/api/v2/read", {"conv": rb_conv}) if rb_conv else {}
    check("TLS 下已读回执回传（B read → A 已读）", r.get("acked", 0) >= 1)
    time.sleep(0.8)
    if a_conv:
        r = http(L["A"], "GET", f"/api/v2/history?conv={a_conv}")
        last = (r.get("items") or [{}])[-1]
        check("TLS 回执落 A 状态", last.get("state") == "read", str(last.get("state")))

    main_plain_dead = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT['A']}/api/v2/ping", timeout=2):
            pass
    except Exception:
        main_plain_dead = True
    check("TLS 下主端口拒绝明文（GUI 只能走接入口）", main_plain_dead)

    print("\n=== 汇总 ===")
    bad = [x for x in results if not x[1]]
    print(f"通过 {len(results) - len(bad)}/{len(results)}")
    for n, ok, d in bad:
        print("  未通过:", n, d)
    if bad:
        sys.exit(1)
finally:
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(1)
    shutil.rmtree(BASE, ignore_errors=True)
