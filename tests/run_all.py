# -*- coding: utf-8 -*-
"""一次性跑完所有验收脚本，只打印每项的汇总行。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ["m1_check", "resend_check", "m2_check", "m3_bridge_check",
         "m4_files_check", "m5_search_progress_check", "m6_runtime_check"]

env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
import os
env = dict(os.environ, **env)

bad = []
for t in TESTS:
    p = ROOT / "tests" / f"{t}.py"
    try:
        r = subprocess.run([sys.executable, "-u", str(p)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=420)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        print(f"{t:<26} TIMEOUT")
        bad.append(t)
        continue
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    summary = ""
    for l in reversed(lines):
        if l.startswith("通过 ") or l.startswith("RESULT:"):
            summary = l
            break
    if not summary:
        summary = (lines[-1] if lines else "(无输出)")[:60]
    ok = r.returncode == 0
    print(f"{t:<26} {'OK ' if ok else 'FAIL'}  {summary}")
    if not ok:
        bad.append(t)
        tail = "\n".join(lines[-8:])
        print("    尾部输出:\n" + "\n".join("      " + x for x in tail.splitlines()))

print()
print(f"全部通过 {len(TESTS) - len(bad)}/{len(TESTS)}" if not bad else f"未通过: {', '.join(bad)}")
sys.exit(0 if not bad else 1)
