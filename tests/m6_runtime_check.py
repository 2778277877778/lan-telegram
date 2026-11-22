# -*- coding: utf-8 -*-
"""运行时可靠性验收（v0.4.1）：僵尸 state、重复实例、孤儿端口、自动拉起、多网卡 IP。

对应线上故障：state.json 记录了已消失的 pid（PID 被回收后仍「存活」），
导致 start_background 误判「已在运行」→ 引擎起不来、界面取不到端口。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "lantelegram.py"
BASE = Path(os.environ.get("TEMP", ".")) / "lt-rt"
KEY = "rtkey0123"


def _free_port():
    """让内核挑一个空闲端口，避免与机器上遗留的引擎撞车。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = _free_port()

results = []
procs = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def env():
    return dict(os.environ, LAN_TG_HOME=str(BASE), PYTHONUTF8="1")


def run(*args, timeout=60):
    return subprocess.run([sys.executable, str(APP), *args], env=env(),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def api(port, path, key=KEY, timeout=3):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v2/{path}")
    req.add_header("X-Sync-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def wait_up(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if api(port, "ping"):
            return True
        time.sleep(0.3)
    return False


def stop_all():
    run("stop")
    time.sleep(0.3)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass


shutil.rmtree(BASE, ignore_errors=True)
BASE.mkdir(parents=True, exist_ok=True)
(BASE / "config.json").write_text(json.dumps({
    "device_id": "rt" + "0" * 30, "device_name": "RT", "port": PORT,
    "key": KEY, "tls": False}), encoding="utf-8")

try:
    print("=== 僵尸 state（pid 被回收） ===")
    # 用当前测试进程的 pid 冒充「已死引擎」：pid 活着但不是引擎
    (BASE / "state.json").write_text(json.dumps({
        "pid": os.getpid(), "port": PORT, "local_port": PORT + 1,
        "started": "2020-01-01 00:00:00"}), encoding="utf-8")
    r = run("serve", "--background")
    out = (r.stdout or "") + (r.stderr or "")
    # 引擎自己启动时输出 JSON；误判时会打印「已在运行」并退出
    check("不误判为已在运行", "已在运行" not in out, out.strip()[:60])
    up = wait_up(PORT + 1)
    check("引擎真的起来了（接入口可 ping）", up)
    st = json.loads((BASE / "state.json").read_text(encoding="utf-8"))
    check("state.json 被改写为真引擎 pid",
          bool(st.get("pid")) and int(st["pid"]) != os.getpid(), f"pid={st.get('pid')}")
    check("state.json 端口正确", int(st.get("port")) == PORT and int(st.get("local_port")) == PORT + 1)

    print("=== 重复调用 start_background ===")
    r = run("serve", "--background")
    out = (r.stdout or "") + (r.stderr or "")
    check("第二次采用已有实例（不再起第二个）", "已在运行" in out or "already" in out or "服务已在运行" in out, out.strip()[:50])

    print("=== 单实例保护 ===")
    before = api(PORT + 1, "ping")
    p2 = subprocess.Popen([sys.executable, str(APP), "serve"], env=env(),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")
    procs.append(p2)
    try:
        out2 = p2.communicate(timeout=15)[0] or ""
        exited = True
    except subprocess.TimeoutExpired:
        out2 = ""
        exited = False
        p2.kill()
    check("第二个 serve 自行退出（不抢同一数据目录）", exited and "已在运行" in out2,
          out2.strip()[:50])
    after = api(PORT + 1, "ping")
    check("原实例未受影响", bool(after) and after.get("device_id") == before.get("device_id"))

    print("=== 端口漂移后仍能找到 ===")
    st = json.loads((BASE / "state.json").read_text(encoding="utf-8"))
    real_local = int(st["local_port"])
    # 人为把 state 指向错误的端口，验证 find_live_engine 的候选扫描能纠回来
    st["local_port"] = real_local + 7
    st["port"] = int(st["port"]) + 7
    (BASE / "state.json").write_text(json.dumps(st), encoding="utf-8")
    r = run("serve", "--background")
    out = (r.stdout or "") + (r.stderr or "")
    adopted = '"already": true' in out.replace(" ", " ") or "已在运行" in out
    check("state 错乱时仍能发现活引擎（不重复启动）", adopted, out.strip()[:60])
    check("活引擎仍在原端口", api(real_local, "ping") is not None)
    st2 = json.loads((BASE / "state.json").read_text(encoding="utf-8"))
    check("被采用后 state.json 已校正回真实端口",
          int(st2.get("local_port")) == real_local and int(st2.get("port")) == PORT,
          f"port={st2.get('port')} local={st2.get('local_port')}")

    print("=== /ping 携带 IP 与端口 ===")
    ping = api(real_local, "ping") or {}
    check("ping 有 ips 列表", isinstance(ping.get("ips"), list) and len(ping["ips"]) >= 1,
          str(ping.get("ips")))
    check("ping 有 port / local_port / pid",
          ping.get("port") == PORT and ping.get("local_port") == real_local and ping.get("pid"))
    ips = ping.get("ips") or []
    check("不含回环/链路本地地址",
          all(not ip.startswith(("127.", "169.254.")) for ip in ips), str(ips))

    print("=== 停止后状态清理 ===")
    run("stop")
    time.sleep(0.8)
    st = json.loads((BASE / "state.json").read_text(encoding="utf-8")) if (BASE / "state.json").exists() else {}
    check("stop 后 state 清空", not st.get("pid"), str(st))
    check("端口已释放", api(PORT + 1, "ping") is None)

    print("\n=== 汇总 ===")
    bad = [x for x in results if not x[1]]
    print(f"通过 {len(results) - len(bad)}/{len(results)}")
    for n, ok, d in bad:
        print("  未通过:", n, d)
    if bad:
        sys.exit(1)
finally:
    stop_all()
    time.sleep(1)
    shutil.rmtree(BASE, ignore_errors=True)
