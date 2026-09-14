#!/usr/bin/env python3
"""在宿主机上原生跑一道 Terminal-Bench 题的一条臂，用 TB 官方判据打分。

    python tb_run_one.py --task jsonl-aggregator --arm gold
    python tb_run_one.py --task jsonl-aggregator --arm none    # 什么都不做，应当 0 分

输出最后一行固定格式，便于汇总：
    <题> cell=<系统> arm=<臂> passed=<n>/<总> rc=<pytest 退出码> s=<秒> port=<ok|unsupported>

★三条纪律写死在这里：
1. 判据用 TB 官方的 `tests/test_outputs.py`，逻辑一个字不改，只做路径重映射。
2. 移植失败要**显式报 unsupported**，绝不静默当成 0 分——那会把「装置没搭起来」
   伪装成「模型没做对」。
3. gold 臂就是官方 `solution.sh`。它在某台上不满分，那一格作废，不参与判模型。
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from port import Unsupported, materialize, remap, shell_path   # noqa: E402

for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def read_instruction(task_dir, root):
    """从 task.yaml 里取出题面。TB 用 `instruction: |-` / `|` / `|+` / `|2` 多种写法，
    早先有个扫描器只认 `|-`，把 4 道题静默漏掉——这里一律用块标量通用规则解析。"""
    txt = (task_dir / "task.yaml").read_text(encoding="utf-8")
    m = re.search(r"(?m)^instruction:\s*([|>][-+0-9]*)?\s*$", txt)
    if not m:
        m2 = re.search(r"(?m)^instruction:\s*(\S.*)$", txt)
        if m2:
            return remap(m2.group(1).strip(), root)
        raise Unsupported("task.yaml 里找不到 instruction")
    lines = txt[m.end():].splitlines()
    body, indent = [], None
    for ln in lines:
        if not ln.strip():
            body.append("")
            continue
        cur = len(ln) - len(ln.lstrip())
        if indent is None:
            indent = cur
        if cur < indent:
            break
        body.append(ln[indent:])
    while body and not body[0].strip():        # 块标量头那一行后面的空行去掉
        body.pop(0)
    return remap(chr(10).join(body).rstrip() + chr(10), root)


ap = argparse.ArgumentParser()
ap.add_argument("--task", required=True)
ap.add_argument("--tb", default=os.environ.get("TB_TASKS", str(HERE / "original-tasks")),
                help="TB 题库目录。公开 runner 仓上由工作流运行时现拉 terminal-bench 后指过来—— 题目数据不进我们自己的仓，既不二次发布带 canary 的基准数据， 也让跑批落在公开仓的免费额度上（私有仓的 Actions 分钟数是计费的， mac 按 10 倍、Windows 按 2 倍，今天就是这么把额度烧穿的）。")
ap.add_argument("--root", default=None, help="宿主机上的工作根，默认 ~/tbroot")
ap.add_argument("--arm", default="gold", choices=["gold", "none", "agent"])
ap.add_argument("--out", default=None)
ap.add_argument("--timeout", type=int, default=1800)
ap.add_argument("--model", default="deepseek-v4.1-flash")
ap.add_argument("--base", default="https://api.llmgateway.io/v1")
a = ap.parse_args()

cell = os.environ.get("XOS_CELL", sys.platform)
task_dir = pathlib.Path(a.tb) / a.task
root = pathlib.Path(a.root or (pathlib.Path.home() / "tbroot")).resolve()
out = pathlib.Path(a.out or (HERE / "_out" / ("%s-%s" % (a.task, a.arm)))).resolve()
out.mkdir(parents=True, exist_ok=True)
log = []
t0 = time.time()


def finish(passed, total, rc, port_state, note=""):
    line = ("%s cell=%s arm=%s passed=%s/%s rc=%s s=%d port=%s%s"
            % (a.task, cell, a.arm, passed, total, rc, int(time.time() - t0),
               port_state, (" note=" + note) if note else ""))
    (out / "meta.json").write_text(json.dumps(
        {"line": line, "passed": passed, "total": total, "rc": rc,
         "port": port_state, "note": note, "log": log},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(line)
    sys.exit(0)


def wipe(p):
    if p.exists():
        def onerr(fn, path, exc):
            try:
                os.chmod(path, 0o700)
                fn(path)
            except Exception:
                pass
        shutil.rmtree(p, onerror=onerr)


if not task_dir.is_dir():
    finish("?", "?", "?", "missing", "题目录不存在")

wipe(root)
root.mkdir(parents=True, exist_ok=True)

# ── 1. 按 Dockerfile 在宿主机上搭环境 ──────────────────────────────────
try:
    from port import check_root
    check_root(root)
    cwd, env = materialize(task_dir, root, log)
except Unsupported as e:
    (out / "port.log").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    finish(0, "?", "-", "unsupported", str(e)[:120])
except Exception as e:
    (out / "port.log").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    finish(0, "?", "-", "porterror", "%s: %s" % (type(e).__name__, str(e)[:100]))

# ★把判据目录也放到 root/tests：TB 在容器里把它挂在 /tests，
#   有的判据会去读同目录下的数据文件（merge-diff 读 /tests/test.json）。
mounted = root / "tests"
wipe(mounted)
if a.arm != "agent":
    shutil.copytree(task_dir / "tests", mounted)
    log.append({"判据目录挂到": str(mounted)})

task_tests = task_dir / "tests"      # ★默认值:gold 臂原来漏了赋值,直接 NameError

# ── 2. 臂 ──────────────────────────────────────────────────────────────
if a.arm == "gold":
    sol = task_dir / "solution.sh"
    if not sol.exists():
        finish(0, "?", "-", "unsupported", "这道题的参考解是 solution.yaml，本移植器暂不支持")
    body = remap(sol.read_text(encoding="utf-8"), root)
    # ★参考解里自己调 apt-get 的行中和掉：装依赖是移植器的活，不是解法的一部分，
    #   而 apt-get 在 mac/Windows 上根本不存在，会让 `set -e` 的解法一上来就退出。
    #   这不是改判据，判据仍是官方 pytest；每中和一行都记账。
    neutral = []
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if "apt-get" in ln or "apt install" in ln:
            neutral.append(ln.strip()[:90])
            lines[i] = "true  # [移植] 原为 apt 装包，由移植器负责：" + ln.strip()[:60]
    if neutral:
        body = chr(10).join(lines) + chr(10)
        log.append({"参考解里被中和的 apt 行": neutral})
    sp = root / "_solution.sh"
    with open(sp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    # ★交给 bash 当参数跑，不靠可执行位：Windows 上没有 POSIX 权限位，
    # 用「当命令调」会在三台上有三种脾气，那是移植噪声不是题目内容。
    p = subprocess.run([shell_path(), str(sp).replace("\\", "/")],
                       cwd=str(cwd), env=env, capture_output=True, timeout=a.timeout)
    log.append({"gold_rc": p.returncode,
                "out": p.stdout.decode("utf-8", "replace")[-800:],
                "err": p.stderr.decode("utf-8", "replace")[-800:]})

elif a.arm == "agent":
    # ★开跑前把「答案」从这台机器上抹掉：agent 有 shell，能翻到仓库里的
    #   solution.sh 与 tests/。判据先读进内存，再把题库目录与 .git 删掉。
    #   有先例：早先两条结果因 agent 读了判据与参考解而作废。
    import io as _io, tarfile as _tar
    _buf = _io.BytesIO()
    with _tar.open(fileobj=_buf, mode="w") as _tf:
        _tf.add(str(task_dir / "tests"), arcname="tests")
    TESTS_TAR = _buf.getvalue()
    instruction = read_instruction(task_dir, root)

    repo_tasks = pathlib.Path(a.tb).resolve()
    victims = [repo_tasks, HERE.parent / ".git", mounted]
    # The upstream clone history also contains solutions and tests.
    if (repo_tasks.parent / ".git").exists():
        victims.append(repo_tasks.parent / ".git")
    for victim in victims:
        wipe(victim)
    leaked = [str(x) for x in victims if x.exists()]
    # COPY . /app can expose evaluator files or reference answers independently.
    leaked.extend(str(x) for x in root.rglob("*")
                  if x.is_file() and x.name in ("solution.sh", "solution.yaml", "test_outputs.py"))
    log.append({"抹掉题库与 .git": "ok" if not leaked else leaked})
    if leaked:
        finish(0, "?", "-", "leak", "答案没抹干净：%s" % leaked)

    pf = out / ".prompt.md"
    pf.write_text(instruction, encoding="utf-8")
    (out / "prompt.rendered.md").write_text(instruction, encoding="utf-8")
    # dsh 的位置:本仓在根目录,实验仓在 tb/ 下。用环境变量指定,找不到再按两处兜底。
    dsh = pathlib.Path(os.environ.get("DSH_DIR") or "")
    if not (dsh / "drive_dsh.py").exists():
        for cand in (HERE / "dsh", HERE.parent / "dsh"):
            if (cand / "drive_dsh.py").exists():
                dsh = cand
                break
    aenv = dict(env,
                DSH_NM=os.environ.get("DSH_NM", str(dsh / "node_modules")),
                DSH_BRIDGE=str(dsh / "bridge.mjs"),
                DSH_CONFIG=str(dsh / "cordis.yaml"),
                DSH_HOME_DIR=str(out / "dsh-home"),
                DSH_SESSION_ROOT=str(out / "dsh-sessions"),
                DSH_RUN_TIMEOUT=str(a.timeout),
                DSH_MAX_TOKENS=os.environ.get("DSH_MAX_TOKENS", "131072"))
    key = os.environ.get("ENVSHIFT_API_KEY", "")
    if not key:
        finish(0, "?", "-", "nokey", "没有 ENVSHIFT_API_KEY，agent 跑不了")
    r = subprocess.run([sys.executable, str(dsh / "drive_dsh.py"), str(cwd), a.model,
                        a.base, key, str(out), str(pf)],
                       env=aenv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=a.timeout + 600)
    (out / "driver.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
    pf.unlink(missing_ok=True)
    log.append({"agent_rc": r.returncode, "题面字数": len(instruction)})
    if r.returncode != 0:
        (out / "port.log").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        finish(0, "?", r.returncode, "agenterror", "driver_failed_requires_trace_review")
    # Preserve the delivered files before official tests can mutate them.
    shutil.make_archive(str(out / "delivered"), "gztar", root_dir=str(cwd))

    # 判据从内存里解出来，不再依赖已被抹掉的题库目录
    tdir_src = out / "_tests_src"
    wipe(tdir_src)
    tdir_src.mkdir(parents=True)
    with _tar.open(fileobj=_io.BytesIO(TESTS_TAR), mode="r") as _tf:
        _tf.extractall(tdir_src)
    task_tests = tdir_src / "tests"
    shutil.copytree(task_tests, mounted)
    log.append({"agent 结束后恢复判据挂载": str(mounted)})
else:
    task_tests = task_dir / "tests"

# ── 3. TB 官方判据（只重映射路径，逻辑不改）──────────────────────────────
tdir = out / "tests"
wipe(tdir)
shutil.copytree(task_tests, tdir)
changed = []
for f in tdir.rglob("*.py"):
    src = f.read_text(encoding="utf-8")
    dst = remap(src, root)
    if dst != src:
        changed.append(f.name)
        f.write_text(dst, encoding="utf-8")
log.append({"判据里被重映射的文件": changed})

env2 = dict(env)
env2["PYTHONDONTWRITEBYTECODE"] = "1"
p = subprocess.run([sys.executable, "-m", "pytest", str(tdir / "test_outputs.py"), "-rA", "-q", "--junitxml", str(out / "pytest.xml")],
                   cwd=str(cwd), env=env2, capture_output=True, timeout=a.timeout)
txt = (p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))
(out / "pytest.log").write_text(txt, encoding="utf-8")
(out / "port.log").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

try:
    report = ET.parse(out / "pytest.xml").getroot()
    cases = list(report.iter("testcase"))
    total = len(cases)
    nerr = sum(c.find("error") is not None for c in cases)
    npass = sum(all(c.find(tag) is None for tag in ("failure", "error", "skipped")) for c in cases)
except (OSError, ET.ParseError) as e:
    finish(0, 0, p.returncode, "gradererror", "missing_or_invalid_junit")
if total == 0 or nerr or p.returncode not in (0, 1):
    finish(npass, total, p.returncode, "gradererror", "pytest_collection_or_execution_error")
finish(npass, total, p.returncode, "ok")
