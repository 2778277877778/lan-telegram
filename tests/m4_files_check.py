# -*- coding: utf-8 -*-
"""M4 验收：文件/图片传输——发送、去重、缩略图、离线补发重传、上限、鉴权。

同机 A/B 双实例；GUI/Rust 只是这套 HTTP 契约的消费者，故在契约层验收。
"""
import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "lantelegram.py"
BASE = Path(os.environ.get("TEMP", ".")) / "lt-m4"
KEY = "m4key0123"
PORT = {"A": 32330, "B": 32340}

# 1x1 PNG（蓝）与 1x1 PNG（红），当原件与缩略图
PNG1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG2 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

results = []
procs = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def home(n):
    return BASE / n


def env(n):
    return dict(os.environ, LAN_TG_HOME=str(home(n)))


def start(n, kill_existing=False):
    h = home(n)
    h.mkdir(parents=True, exist_ok=True)
    cfgf = h / "config.json"
    cfg = {"device_id": f"dev{n}{'0' * 28}"[:32], "device_name": n,
           "port": PORT[n], "key": KEY, "tls": False}
    if cfgf.exists():
        cfg.update({k: v for k, v in json.loads(cfgf.read_text("utf-8-sig")).items()
                    if k in ("device_id",)})
        cfg["port"] = PORT[n]; cfg["key"] = KEY; cfg["tls"] = False
    cfgf.write_text(json.dumps(cfg), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(APP), "serve"], env=env(n),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


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


def api(port, method, path, body=None, key=KEY, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    req.add_header("X-Sync-Key", key)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read()
            try:
                return json.loads(raw)
            except Exception:
                return {"bytes": raw}
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def run_cli(n, *args):
    return subprocess.run([sys.executable, str(APP), *args], env=env(n),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


shutil.rmtree(BASE, ignore_errors=True)
pA = pB = None
try:
    pA = start("A")
    pB = start("B")
    print("=== 启动 ===")
    check("A/B 就绪", wait_up(PORT["A"]) and wait_up(PORT["B"]))

    (BASE / "img.png").write_bytes(PNG1)
    thumb_b64 = base64.b64encode(PNG2).decode()

    print("=== 发送图片 ===")
    r = api(PORT["A"] + 1, "POST", "/api/v2/send",
             {"to": f"127.0.0.1:{PORT['B']}", "path": str(BASE / "img.png"),
              "thumb_b64": thumb_b64})
    msg = r.get("message") or {}
    mid = msg.get("id", "")
    conv = msg.get("conv", "")
    check("A 发图成功（delivered）",
          r.get("ok") and msg.get("kind") == "image"
          and msg.get("body", {}).get("name") == "img.png" and r.get("state") in ("delivered", "sent"),
          str(r.get("state")))

    # B 侧该会话的键是 dm:<A 设备id>（会话 id 相对于每个本机）
    conv_b = f"dm:{('devA' + '0' * 28)[:32]}"
    r = api(PORT["B"] + 1, "GET", f"/api/v2/history?conv={conv_b}")
    items = r.get("items") or []
    got = items and items[-1]["id"] == mid and items[-1]["body"].get("size") == len(PNG1)
    check("B 历史出现图片消息（kind/body 正确）", bool(got))

    r = api(PORT["B"] + 1, "GET", f"/api/v2/file?msg={mid}")
    check("B /file 原件字节一致", r.get("bytes") == PNG1)
    r = api(PORT["B"] + 1, "GET", f"/api/v2/file?msg={mid}&thumb=1")
    check("B /file 缩略图字节一致", r.get("bytes") == PNG2)

    print("=== 幂等/上限/鉴权 ===")
    c = http.client.HTTPConnection("127.0.0.1", PORT["B"], timeout=5)
    headers = {"X-Sync-Key": KEY, "Content-Type": "application/octet-stream",
               "X-Msg-Id": mid, "X-Kind": "image", "X-Ts": str(int(time.time() * 1000)),
               "X-From": f"devA{'0' * 28}"[:32], "X-Name": "img.png",
               "X-Size": str(len(PNG2)), "Content-Length": str(len(PNG2))}
    c.request("POST", "/api/v2/upload", body=PNG2, headers=headers)
    body = json.loads(c.getresponse().read())
    c.close()
    check("重复投递判定 duplicate 且不覆盖原件", body.get("duplicate") is True)
    orig = api(PORT["B"] + 1, "GET", f"/api/v2/file?msg={mid}")
    check("重复后原件仍为第一次的字节", orig.get("bytes") == PNG1)

    c = http.client.HTTPConnection("127.0.0.1", PORT["B"], timeout=5)
    c.request("POST", "/api/v2/upload", body=b"x",
              headers={"X-Sync-Key": KEY, "X-Msg-Id": "f" * 32, "X-Kind": "file",
                       "X-Ts": "1", "X-From": "x" * 32, "X-Name": "huge",
                       "X-Size": str(UPLOAD_MAX := 501 * 1024 * 1024),
                       "Content-Length": "1"})
    resp = c.getresponse()
    check("超 500MB 上限 413 拒绝", resp.status == 413, f"HTTP {resp.status}")
    c.close()

    r = api(PORT["B"] + 1, "GET", f"/api/v2/file?msg={mid}", key="BAD")
    check("坏密钥 401", "密钥不匹配" in (r.get("error") or ""))

    print("=== 离线补发重传 ===")
    pB.terminate(); pB.wait(timeout=10)
    time.sleep(0.5)
    (BASE / "doc.bin").write_bytes(b"hello-lan-telegram" * 100)
    r = run_cli("A", "send", "--target", f"127.0.0.1:{PORT['B']}", "--file", str(BASE / "doc.bin"))
    queued = "入队" in (r.stdout or r.stderr)
    check("CLI --file 离线入队", queued, (r.stdout or r.stderr).strip()[:40])
    pB = start("B")
    check("B 重新上线", wait_up(PORT["B"]))
    ok = False
    end = time.time() + 30
    while time.time() < end:
        time.sleep(2)
        r = run_cli("B", "history", "--with", "A")
        if "doc.bin" in (r.stdout or ""):
            ok = True
            break
    check("补发后 B 收到附件（CLI history 展示）", ok)
    if ok:
        lines = [l for l in (r.stdout or "").splitlines() if "doc.bin" in l]
        # 从 B 侧导出验证字节
        r2 = run_cli("B", "history", "--with", "A")
        # 拿 id：直接查库更快
        import sqlite3
        con = sqlite3.connect(str(home("B") / "messages.db"))
        row = con.execute("SELECT id FROM messages WHERE kind='file' LIMIT 1").fetchone()
        con.close()
        r3 = run_cli("B", "getfile", "--msg", row[0], "--out", str(BASE / "doc-out.bin"))
        saved = (BASE / "doc-out.bin").exists() and (BASE / "doc-out.bin").read_bytes() == b"hello-lan-telegram" * 100
        check("getfile 导出补发附件字节一致", saved)

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
