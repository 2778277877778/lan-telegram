# -*- coding: utf-8 -*-
"""M5-A 验收：本地搜索（含转义/权限/上限）与发送进度事件。

两个引擎实例 A/B；搜索走 A 的本机接入口，进度事件取自 A 的事件流。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "lantelegram.py"
BASE = Path(os.environ.get("TEMP", ".")) / "lt-m5a"
KEY = "m5akey01"
PORT = {"A": 32350, "B": 32360}

results = []
procs = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def home(n):
    return BASE / n


def env(n):
    return dict(os.environ, LAN_TG_HOME=str(home(n)))


def start(n):
    h = home(n)
    h.mkdir(parents=True, exist_ok=True)
    (h / "config.json").write_text(json.dumps({
        "device_id": f"dev{n}{'0' * 28}"[:32], "device_name": n,
        "port": PORT[n], "key": KEY, "tls": False}), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(APP), "serve"], env=env(n),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


def wait_up(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v2/ping", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def api(port, path, key=KEY, timeout=30):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v2/{path}")
    req.add_header("X-Sync-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def post(port, path, body, key=KEY, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v2/{path}", method="POST")
    req.add_header("X-Sync-Key", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, json.dumps(body).encode(), timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}


def run_cli(n, *args):
    return subprocess.run([sys.executable, str(APP), *args], env=env(n),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


shutil.rmtree(BASE, ignore_errors=True)
try:
    start("A")
    start("B")
    print("=== 启动 ===")
    check("A/B 就绪", wait_up(PORT["A"]) and wait_up(PORT["B"]))

    print("=== 造数据 ===")
    r1 = post(PORT["B"] + 1, "send", {"to": f"127.0.0.1:{PORT['A']}", "text": "接口文档我放共享盘了"})
    r2 = post(PORT["A"] + 1, "send", {"to": f"127.0.0.1:{PORT['B']}", "text": "接口我今晚对一遍"})
    r3 = post(PORT["A"] + 1, "send", {"to": f"127.0.0.1:{PORT['B']}", "text": "另外那个 100% 进度条要加"})
    r4 = post(PORT["A"] + 1, "send", {"to": f"127.0.0.1:{PORT['B']}", "text": "price_100%_done"})
    ok_seed = all(x.get("ok") for x in (r1, r2, r3, r4))
    check("四条消息投递成功", ok_seed, str([x.get("state") for x in (r1, r2, r3, r4)]))
    time.sleep(0.5)

    print("=== 搜索 API ===")
    r = api(PORT["A"] + 1, "search?q=" + urllib.parse.quote("接口"))
    items = r.get("items") or []
    check("关键词「接口」命中 2 条", r.get("ok") and r.get("count") == 2, f"count={r.get('count')}")
    check("结果带会话与正文结构",
          bool(items) and all(isinstance(i.get("body"), dict) for i in items)
          and "接口" in (items[0]["body"].get("text") or ""))
    r = api(PORT["A"] + 1, "search?q=" + urllib.parse.quote("今晚对一遍"))
    check("长短语精确命中 1 条", (r.get("count") or 0) == 1)
    r = api(PORT["A"] + 1, "search?q=" + urllib.parse.quote("不存在的词xyz"))
    check("无结果返回空且 ok", r.get("ok") and r.get("count") == 0)

    print("=== 通配符与上限 ===")
    r = api(PORT["A"] + 1, "search?q=" + urllib.parse.quote("%"))
    # body JSON 里含有 % 的只有最后一条；若 % 未转义则会命中全部 4 条
    check("LIKE 通配符 % 被转义（只命中含 % 的消息）",
          (r.get("count") or 0) == 2, f"count={r.get('count')}")
    r = api(PORT["A"] + 1, "search?q=" + urllib.parse.quote("_"))
    # 只有 "price_100%_done" 含下划线；若 _ 未转义（LIKE 单字符通配）会命中全部 4 条
    check("下划线 _ 被转义（只命中含 _ 的 1 条）", (r.get("count") or 0) == 1,
          f"count={r.get('count')}")
    r = api(PORT["A"] + 1, "search?q=" + urllib.parse.quote("接口") + "&limit=1")
    check("limit 生效", len(r.get("items") or []) == 1)
    r = api(PORT["A"] + 1, "search?q=")
    check("空查询 400", "缺少查询词" in (r.get("error") or ""))
    r = api(PORT["A"] + 1, "search?q=" + urllib.parse.quote("接口"), key="BAD")
    check("坏密钥 401", "密钥不匹配" in (r.get("error") or ""))

    print("=== CLI 搜索 ===")
    r = run_cli("A", "search", "--query", "接口")
    out = r.stdout or ""
    check("CLI 输出命中行与 conv/id", "含「接口」的消息" in out and "conv:" in out,
          out.strip().splitlines()[0] if out.strip() else "无输出")
    r = run_cli("A", "search", "--query", "不存在xyz")
    check("CLI 无结果提示", "没有找到" in (r.stdout or ""))

    print("=== 发送进度事件 ===")
    big = BASE / "big.bin"
    big.write_bytes(os.urandom(3 * 1024 * 1024))     # 3MB，必然分块
    r = post(PORT["A"] + 1, "send",
             {"to": f"127.0.0.1:{PORT['B']}", "path": str(big), "client_ref": "ref-abc"})
    check("3MB 文件投递成功", r.get("ok") and r.get("state") in ("delivered", "sent"),
          str(r.get("state")))
    time.sleep(0.5)
    ev = api(PORT["A"] + 1, "events?since=0&timeout=0")
    prog = [e for e in (ev.get("events") or []) if e.get("type") == "progress"]
    mids = {p["data"].get("id") for p in prog}
    pcts = [p["data"].get("percent") for p in prog]
    sends = [p["data"].get("sent") for p in prog]
    check("进度事件存在且同一消息", len(prog) >= 2 and len(mids) == 1, f"{len(prog)} 条")
    check("百分比单调到 100", pcts == sorted(pcts) and pcts[-1] == 100, str(pcts[:6]) + "…" + str(pcts[-2:]))
    check("字节数单调递增", sends == sorted(sends), str(sends[:5]))
    check("client_ref 透传", any(p["data"].get("client_ref") == "ref-abc" for p in prog))
    check("总大小正确", all(p["data"].get("total") == 3145728 for p in prog))

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
