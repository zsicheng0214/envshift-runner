#!/usr/bin/env python3
"""EnvShift 裸机执行器(不依赖 Docker;给 GitHub 的 macOS/Windows/Linux runner 用)。
三阶段流程,与容器版执行器一致。"""
import argparse, json, os, pathlib, shutil, stat, subprocess, sys, tempfile, time
for _st in (sys.stdout, sys.stderr):
    try: _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
BIN = {
       "E10-dupes-extend": ("dupes.py", "E10"),
       "E11-export-extend": ("export.sh", "E11"),
       "E12-publish-extend": ("publish.sh", "E12"),
       "E3-archive-audit": ("audit", "E3"),
       "E4-vault-compliance": ("vault-archive", "E4"),
       "E5-retention-plan": ("retain", "E5"),
       "E6-contact-merge": ("merge.sh", "E6"),
       "E7-publish-shared": ("publish", "E7"),
       "E8-report-extend": ("report.py", "E8"),
       "E9-latest-extend": ("latest.py", "E9"),
       "EXAMPLE-git-newline": ("digest.py", "EG1"),
       "S01-ledger-pipefail": ("roll.sh", "S01"),
       "S10-ledger-ansiquote": ("roll.sh", "S10"),
       "S11-batch-ansiquote": ("sweep.sh", "S11"),
       "S12-spool-ansiquote": ("ship.sh", "S12"),
       "S13-ledger-echobs": ("roll.sh", "S13"),
       "S14-batch-echobs": ("sweep.sh", "S14"),
       "S15-spool-echobs": ("ship.sh", "S15"),
       "V30-release-dot": ("run_pipeline.sh", "V30"),
       "V31-intake-dot": ("run_pipeline.sh", "V31"),
       "V32-backup-dot": ("run_pipeline.sh", "V32"),
       "V33-release-pathcase": ("run_pipeline.sh", "V33"),
       "V34-intake-pathcase": ("run_pipeline.sh", "V34"),
       "V35-backup-pathcase": ("run_pipeline.sh", "V35"),
       "V36-release-textnl": ("run_pipeline.sh", "V36"),
       "V37-intake-textnl": ("run_pipeline.sh", "V37"),
       "V38-backup-textnl": ("run_pipeline.sh", "V38"),
       "W01-release-pipeline": ("run_pipeline.sh", "W01"),
       "W10-release-dot": ("run_pipeline.sh", "W10"),
       "W11-intake-dot": ("run_pipeline.sh", "W11"),
       "W12-release-tmproot": ("run_pipeline.sh", "W12"),
       "W13-intake-tmproot": ("run_pipeline.sh", "W13"),
       "W14-release-stamp": ("run_pipeline.sh", "W14"),
       "W15-intake-stamp": ("run_pipeline.sh", "W15"),
       "W16-release-envcase": ("run_pipeline.sh", "W16"),
       "W17-intake-envcase": ("run_pipeline.sh", "W17"),
       "W18-release-pathcase": ("run_pipeline.sh", "W18"),
       "W19-intake-pathcase": ("run_pipeline.sh", "W19"),
       "W20-release-subdecode": ("run_pipeline.sh", "W20"),
       "W21-intake-subdecode": ("run_pipeline.sh", "W21"),
       "W22-release-spawn": ("run_pipeline.sh", "W22"),
       "W23-intake-spawn": ("run_pipeline.sh", "W23"),
       "W24-release-autocrlf": ("run_pipeline.sh", "W24"),
       "W25-intake-autocrlf": ("run_pipeline.sh", "W25"),
       "X1-index-extend": ("index.py", "X1"),
       "X105-ticket-m25": ("oneline.sh", "X105"),
       "X106-stock-m25": ("tagline.sh", "X106"),
       "X15-archive-m5": ("emit.py", "X15"),
       "X17-ticket-m5": ("dispatch.py", "X17"),
       "X18-stock-m5": ("issue.py", "X18"),
       "X2-emit-extend": ("emit.py", "X2"),
       "X3-tally-extend": ("tally.py", "X3"),
       "X31-archive-m10": ("rollup.py", "X31"),
       "X4-pick-extend": ("pick.py", "X4"),
       "X49-ticket-m14": ("verify_sizes.py", "X49"),
       "X5-export-extend": ("export.sh", "X5"),
       "X54-stock-m16": ("runner.py", "X54"),
       "X57-ticket-m20": ("tidy.py", "X57"),
       "X58-stock-m20": ("reap.py", "X58"),
       "X6-rollup-extend": ("rollup.py", "X6"),
       "X60-signup-m12": ("rollcall.py", "X60"),
       "X61-ticket-m12": ("brief.py", "X61"),
       "X66-stock-m17": ("freshness.py", "X66"),
       "X76-signup-m15": ("compose.py", "X76"),
       "X78-stock-m15": ("weave.py", "X78"),
       "X79-archive-m9": ("loadconf.py", "X79"),
       "X80-signup-m9": ("setup_env.py", "X80"),
       "Y02-git-filemode": ("perms.py", "Y02"),
       "Y03-tar-owner": ("attest.py", "Y03")}
ap = argparse.ArgumentParser()
ap.add_argument("--task", required=True); ap.add_argument("--arm", default="oracle")
ap.add_argument("--data", default="/data"); ap.add_argument("--app", default="/app")
ap.add_argument("--out", default="out"); ap.add_argument("--model", default="deepseek-v4-pro")
ap.add_argument("--base", default="https://api.llmgateway.io/v1"); ap.add_argument("--cell-env", default="{}")
ap.add_argument("--timeout", type=int, default=3600); ap.add_argument("--sudo-verify", action="store_true")
a = ap.parse_args()
here = pathlib.Path(__file__).resolve().parent; td = here / "tasks" / a.task
_sp = td / "task.json"
if _sp.exists():
    _s = json.loads(_sp.read_text(encoding="utf-8")); binname, venv = _s["tool"], _s["venv"]
else:
    binname, venv = BIN[a.task]      # 老题兜底
 data, app = pathlib.Path(a.data), pathlib.Path(a.app)
if os.name == "nt" and a.task in ("E7-publish-shared", "E12-publish-extend"):
    print(f"{a.task} cell={os.environ.get('XOS_CELL','?')} arm={a.arm} reward=NA 诊断=NA (Windows 无 POSIX 权限模型:坐标无定义)"); sys.exit(0)
out = pathlib.Path(a.out).resolve(); out.mkdir(parents=True, exist_ok=True)
cell_env = json.loads(a.cell_env); t0 = time.time()
def wipe(p):
    p.mkdir(parents=True, exist_ok=True)
    for c in p.iterdir(): shutil.rmtree(c, ignore_errors=True) if c.is_dir() else c.unlink(missing_ok=True)
wipe(data); wipe(app)
# 运行材料只存在于内存,agent 阶段不落盘。
import io, tarfile
_buf = io.BytesIO()
with tarfile.open(fileobj=_buf, mode="w") as _tf: _tf.add(str(td / "verifier"), arcname="verifier")
VERIFIER_TAR = _buf.getvalue()
def _rm(p):
    # 只读文件要先去只读位再删
    def _onerr(fn, path, exc):
        os.chmod(path, stat.S_IWRITE); fn(path)
    if p.exists(): shutil.rmtree(p, onerror=_onerr)
MKFIX_SRC = (td / "mkfixture.py").read_text(encoding="utf-8")
PROMPT_SRC = (td / "prompt.md").read_text(encoding="utf-8")
ARM_DIR_TAR = None
if a.arm not in ("agent", "null"):
    _b2 = io.BytesIO()
    with tarfile.open(fileobj=_b2, mode="w") as _tf: _tf.add(str(td / "arms" / a.arm), arcname="arm")
    ARM_DIR_TAR = _b2.getvalue()
if a.arm == "agent":
    _rm(here / "tasks"); _rm(here / ".git")
    for _f in here.glob("*.md"): _f.unlink(missing_ok=True)
    for _f in here.glob("*.py"):
        if _f.resolve() != pathlib.Path(__file__).resolve():
            try: _f.unlink()
            except OSError: pass
    assert not (here / "tasks").exists() and not (here / ".git").exists(), "materials not cleared"
# 1) fixture:不施加格子环境
_mk = pathlib.Path(tempfile.mkdtemp(prefix="mkfix-")) / "mkfixture.py"; _mk.write_text(MKFIX_SRC, encoding="utf-8")
r = subprocess.run([sys.executable, str(_mk), str(data)], capture_output=True, text=True,
                   env={**os.environ, f"{venv}_APP": str(app)})   # 从内存写回临时目录跑;E8/E9 的现成工具由 fixture 写进 app 根
shutil.rmtree(_mk.parent, ignore_errors=True)
(out / "fixture.log").write_text(r.stdout + r.stderr)
# ★记下现成工具的原样:agent 臂交完之后要比对,把「根本没改」与「机制翻转」分开。
# 这两种在分数上都是 reward=0,混在一起会让「没干完活」被当成环境敏感度信号。
_orig_tool = None
try:
    _orig_tool = (app / binname).read_bytes()
except Exception:
    pass
if r.returncode != 0: print(f"{a.task} arm={a.arm} FIXTURE-FAIL {r.stderr[-200:]}"); sys.exit(4)
# 2) 臂
agent_rc = 0; env_cell = dict(os.environ, **cell_env)
if a.arm == "agent":
    dsh = here / "dsh"; nm = dsh / "node_modules"
    prompt = out / ".prompt.md"
    # ★题面里写死的 /data /app 按本 OS 的真实根目录渲染(mac 根目录只读、Windows 无根目录)——这本身是 OS 坐标的一部分
    def _p(pth): return str(pathlib.Path(pth).resolve()).replace("\\", "/")
    txt = PROMPT_SRC.replace("/data", _p(data)).replace("/app", _p(app))
    prompt.write_text(txt, encoding="utf-8"); (out / "prompt.rendered.md").write_text(txt, encoding="utf-8")
    env = dict(env_cell, DSH_NM=str(nm), DSH_BRIDGE=str(dsh / "bridge.mjs"), DSH_CONFIG=str(dsh / "cordis.yaml"),
               DSH_HOME_DIR=str(out / "dsh-home"), DSH_SESSION_ROOT=str(out / "dsh-sessions"),
               DSH_RUN_TIMEOUT=str(a.timeout), DSH_MAX_TOKENS=os.environ.get("DSH_MAX_TOKENS", "131072"))
    key = os.environ.get("ENVSHIFT_API_KEY", "")
    r = subprocess.run([sys.executable, str(dsh / "drive_dsh.py"), str(app), a.model, a.base, key, str(out), str(prompt)],
                       cwd=str(app), env=env, capture_output=True, text=True, timeout=a.timeout + 300)
    (out / "driver.log").write_text(r.stdout + r.stderr); agent_rc = r.returncode; prompt.unlink(missing_ok=True)
    # ★把 agent 实际交付的文件打出来:产物在加密 artifact 里,不打印就只能靠猜它写了什么。
    _dp = app / binname
    if _dp.exists():
        try:
            _txt = _dp.read_text(encoding="utf-8", errors="replace")
        except Exception as _e:
            _txt = "<读不出 %s>" % type(_e).__name__
        print("  DELIVERED-BEGIN %s (%d 字节)" % (binname, len(_txt)), file=sys.stderr)
        for _ln in _txt.splitlines()[:80]:
            print("  DELIVERED| " + _ln[:200], file=sys.stderr)
        print("  DELIVERED-END", file=sys.stderr)
    # ★agent 秒退时把 driver 日志尾部露出来:日志本体在加密 artifact 里,不打出来就只能看到
    # 「agent_rc=0、agent_s=2」这种「没报错但什么也没做」的哑失败,无从判断是模型还是装置。
    if int(time.time() - t0) < 20 or agent_rc != 0:
        _dl = (r.stdout + r.stderr).strip().splitlines()
        for _ln in _dl[-12:]:
            print("  DRIVER-TAIL " + _ln[:200], file=sys.stderr)
elif a.arm != "null":
    if ARM_DIR_TAR is None: print(f"{a.task} arm={a.arm} NO-SUCH-ARM"); sys.exit(2)
    _at = pathlib.Path(tempfile.mkdtemp(prefix="arm-"))
    with tarfile.open(fileobj=io.BytesIO(ARM_DIR_TAR), mode="r") as _tf: _tf.extractall(_at)
    shutil.copytree(_at / "arm", app, dirs_exist_ok=True); shutil.rmtree(_at, ignore_errors=True)
    b = app / binname; b.chmod(b.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
t1 = time.time()
# 3) 评分阶段
tests = pathlib.Path(tempfile.mkdtemp(prefix="tests-"))
with tarfile.open(fileobj=io.BytesIO(VERIFIER_TAR), mode="r") as _tf: _tf.extractall(tests)
tests = tests / "verifier"
logdir = out / "verifier-log"; logdir.mkdir(exist_ok=True)
venv_env = dict(env_cell, **{f"{venv}_LOG": str(logdir), f"{venv}_BIN": str(app / binname), f"{venv}_ROOT": str(data)})
cmd = [sys.executable, str(tests / "check.py")]
if a.sudo_verify and os.name == "posix" and shutil.which("sudo"): cmd = ["sudo", "-E", "--preserve-env=PATH"] + cmd
r = subprocess.run(cmd, env=venv_env, capture_output=True, text=True, timeout=1800)
(out / "verifier.log").write_text(r.stdout + r.stderr)
reward = (logdir / "reward.txt").read_text().strip() if (logdir / "reward.txt").exists() else "?"
tr = json.loads((logdir / "trace_results.json").read_text()) if (logdir / "trace_results.json").exists() else {}
delivered = "na"
if a.arm == "agent":
    try:
        _now = (app / binname).read_bytes()
        delivered = "unchanged" if (_orig_tool is not None and _now == _orig_tool) else "modified"
    except Exception:
        delivered = "missing"
line = f"{a.task} cell={os.environ.get('XOS_CELL','?')} arm={a.arm} reward={reward} 诊断={tr.get('points','?')}/{tr.get('total','?')} agent_rc={agent_rc} vrc={r.returncode} agent_s={int(t1-t0)} verify_s={int(time.time()-t1)} delivered={delivered}"
print(line)
if reward != "1":
    _vl = (out / "verifier.log").read_text(encoding="utf-8", errors="replace") if (out / "verifier.log").exists() else ""
    for _ln in _vl.strip().splitlines()[-6:]:
        # ★打到 stderr:工作流用 `| tail -1` 只留结果行,明细走 stdout 会把结果行顶掉,
        # 整轮门一因此只收到 oracle 一条、看起来像「naive 没翻」。
        print("  VERIFIER-TAIL " + _ln[:200], file=sys.stderr)
(out / "meta.json").write_text(json.dumps({"line": line, "reward": reward, "trace": tr, "cell_env": cell_env}, ensure_ascii=False, indent=2))
