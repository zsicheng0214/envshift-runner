#!/usr/bin/env python3
"""把 Terminal-Bench 的题从 Linux 容器移植到「三个系统的宿主机原生」跑。

为什么要这件事(2026-09-14 定的方向):
  我自己造的 52 道题门二零区分度。查了 TB 才看明白病根——
  TB 全部跑在 Linux 容器里,它**根本不测跨 OS**,它的失败率来自**活太难**
  (TB 3.0 最强模型 42.7%);而我的题活太简单,基线失败率≈0。
  环境效应是乘在基线失败率上的,乘 0 还是 0。
  所以换一条路:拿 TB 那个难度的真活,不额外埋任何机制,同一道题在三台上原生跑,
  看它自己在哪儿散架。

★纪律:
1. **判据一个字不改**,仍是 TB 官方的 `tests/test_outputs.py` + pytest。
   唯一的改动是**路径重映射**,而且三台**用完全一样的映射**,所以不构成跨系统混淆。
   容器里的 `/app` 在 mac 上建不出来(根卷只读)、在 Windows 上不存在,不映射就一道也跑不了。
2. **先过 gold 闸门再谈模型**:官方参考解在某台上跑不满分,那一格就是移植没做对,
   不能拿来判 agent。哪台 gold 挂了就记下来,要么修移植,要么弃那一格,绝不静默算进分母。
3. 移植过程只做机械替换,不改题意、不改判据逻辑。改了什么全部落日志。
"""
import argparse
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 容器里的绝对路径 → 宿主机上的可写位置。三台共用同一套映射。
# ★/tmp 必须也映射进来:容器里往 /tmp 写是常见写法(deterministic-tarball 就有
#   `COPY setup_source_tree.sh /tmp/`),不映射就会落到宿主机的真 /tmp 上,
#   而本项目的工作区正好在那底下——第一版就是这么把整个 scratchpad 改写了一遍。
# /tests 是 TB 在容器里挂判据目录的位置，判据自己会读 /tests/xxx.json
MAPPED = ("/app", "/data", "/logs", "/srv", "/opt/task", "/tmp", "/tests")


def check_root(root):
    """工作根本身不许落在任何被映射的前缀下面。

    ★踩过:把根设成 /tmp/tbroot,而 /tmp 也在映射表里,于是根路径自己被反复替换成
    /private/private/tmp/tbroot/tmp/tbroot/...。自指替换,当场断言掉。
    """
    s = str(pathlib.Path(root).resolve()).replace(chr(92), "/")
    for m in MAPPED:
        if s == m or s.startswith(m + "/"):
            raise Unsupported("工作根 %s 落在被映射的前缀 %s 下,换一个根(比如 ~/tbroot)" % (s, m))


def remap(text, root):
    """把容器绝对路径换成宿主机路径。

    ★只在**路径边界**上替换，不能做裸子串替换：
      脚本里的相对路径 `.cache/data` 会被裸替换改成 `.cache/<root>/data`，
      症状是 touch 报 No such file or directory，看着像题目坏了，其实是我换错了。
      规则：被映射的前缀前面不能紧挨着 字母数字/点/斜杠/连字符，后面必须是
      路径分隔符、引号、空白或行尾。
    """
    out = text
    for seg in sorted(MAPPED, key=len, reverse=True):
        repl = str(root / seg.lstrip("/")).replace(chr(92), "/")
        pat = r"(?<![A-Za-z0-9_.\-/])" + re.escape(seg) + r"(?=[/\s\"'\):;,]|$)"
        out = re.sub(pat, lambda m, r=repl: r, out)   # 同上：替换串绝不交给 re 解释
    return out


class Unsupported(Exception):
    pass


# 只对「脚本类」文件做内容重映射。数据文件不碰——万一判据比对的正是它的字节，
# 改了就成了我自己制造的差异。每改一处都记账。
REMAP_SUFFIX = {".py", ".sh", ".bash", ".yaml", ".yml", ".cfg", ".ini", ".toml", ".mk"}


def remap_tree(dst, root, log):
    """把刚 COPY 进去的脚本里硬编码的容器路径也换掉。

    ★这是本移植器最容易出错、也最必要的一步:
      `/app` 不只写在 Dockerfile 与判据里,还写在题目自带的脚本内容里
      (compute_seq.py 里的 /app/input/salt.txt、setup_source_tree.sh 里的 mkdir /app)。
      不改就是「宿主机上根本没有那个目录」,而 mac 的根卷只读,建都建不出来。
    """
    # ★★硬护栏:只许在工作根以内改文件。
    # 第一版没有这道闸,遇到落在 /tmp 的 COPY 就把宿主机整个 /tmp 递归改写了,
    # 连本项目自己的源码和 TB 的原始素材一起改坏。越界写必须当场拒绝,不能只靠调用方小心。
    try:
        r = root.resolve()
        d = dst.resolve()
        inside = (d == r) or (str(d).startswith(str(r) + os.sep))
    except OSError:
        inside = False
    if not inside:
        log.append({"拒绝越界重映射": str(dst), "工作根": str(root)})
        return

    hit = []
    targets = [dst] if dst.is_file() else list(dst.rglob("*")) if dst.is_dir() else []
    for f in targets:
        if not f.is_file() or f.suffix.lower() not in REMAP_SUFFIX:
            continue
        try:
            src = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out = remap(src, root)
        if out != src:
            with open(f, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(out)
            hit.append(str(f.relative_to(root)) if str(f).startswith(str(root)) else f.name)
    if hit:
        log.append({"脚本内容里重映射了路径": hit})


def parse_dockerfile(path):
    """把 Dockerfile 拆成指令序列。只认本批题真的用到的那几种。

    ★支持 heredoc 形式的 COPY(intrusion-detection 用它内联日志内容):
        COPY <<EOT /app/logs/auth.log
        ...正文...
        EOT
      不识别的话,正文每一行都会被当成指令,报出「没见过的指令 APR」这种莫名其妙的错。
    """
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    lines, buf = [], ""
    i, n = 0, len(raw_lines)
    while i < n:
        s = raw_lines[i].rstrip()
        m = re.match(r"\s*COPY\s+<<-?(\w+)\s+(\S+)\s*$", s)
        if m and not buf:
            tag, dest = m.group(1), m.group(2)
            body = []
            i += 1
            while i < n and raw_lines[i].strip() != tag:
                body.append(raw_lines[i])
                i += 1
            i += 1
            lines.append(("HEREDOC", dest, chr(10).join(body) + chr(10)))
            continue
        i += 1
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        if buf:
            buf += " " + s.strip()
        else:
            buf = s.strip()
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)

    steps = []
    for ln in lines:
        if isinstance(ln, tuple):
            steps.append(ln)
            continue
        head = ln.split(None, 1)[0].upper()
        rest = ln[len(head):].strip()
        if head in ("FROM", "ENV", "ARG", "LABEL", "USER", "EXPOSE", "CMD", "ENTRYPOINT"):
            steps.append((head, rest))
        elif head in ("WORKDIR", "COPY", "RUN"):
            steps.append((head, rest))
        else:
            raise Unsupported("没见过的 Dockerfile 指令 %s" % head)
    return steps


def run_sh(cmd, cwd, env, log):
    """在宿主机上执行一条 RUN。Windows 上绝不用裸 bash（System32 那个是 WSL 入口）。"""
    sh = shell_path()
    p = subprocess.run([sh, "-lc", cmd], cwd=str(cwd), env=env,
                       capture_output=True, timeout=1800)
    log.append({"cmd": cmd[:400], "rc": p.returncode,
                "out": p.stdout.decode("utf-8", "replace")[-600:],
                "err": p.stderr.decode("utf-8", "replace")[-600:]})
    return p.returncode


def shell_path():
    if os.name != "nt":
        return shutil.which("bash") or "/bin/bash"
    BS = chr(92)
    pf = os.environ.get("ProgramFiles", "C:" + BS + "Program Files")
    for sub in (("Git", "bin", "bash.exe"), ("Git", "usr", "bin", "bash.exe")):
        c = os.path.join(pf, *sub)
        if os.path.exists(c):
            return c
    p = shutil.which("bash")
    if p and "system32" not in p.lower():
        return p
    raise Unsupported("Windows 上找不到 Git bash")


# ── apt 装的包 → 宿主机上怎么办 ────────────────────────────────────────
# ★不再写死「这台有没有」。上一版我按印象填表，结果实测打脸：
#   Windows 上 tree / bc / zstd / man-db 其实全都有，我却填了「没有」，
#   白白把 4 道题判成移植不成立。
#   现在的规则:先看这台机器上那个命令在不在(shutil.which),在就跳过;
#   不在才按平台走安装配方;没有配方才如实报缺。
PKG_CMD = {            # 包名 → 它提供的命令(用来判断「已经有了」)
    "tmux": None, "asciinema": None,          # TB 自己的录制设施，不是题目依赖
    "curl": "curl", "git": "git", "bash": "bash", "grep": "grep",
    "coreutils": "ls", "findutils": "find", "tar": "tar", "file": "file",
    "python3": "python3", "python3-pip": "pip3", "python3-venv": "python3",
    "tree": "tree", "jq": "jq", "bc": "bc", "zstd": "zstd",
    "dos2unix": "dos2unix", "man-db": "mandb", "locales": "locale",
}
INSTALLER = {          # 这台没有时怎么装
    "darwin": "brew install %s",
    "linux": "sudo apt-get install -y -qq %s",
    "win32": None,     # Windows 上装东西慢且多半用不着(实测该有的都有)
}
PKG_NATIVE_NAME = {"man-db": None, "locales": None,          # 这两个在非 Linux 上没有对应包
                   "python3-pip": None, "coreutils": None, "findutils": None}


def _plat():
    return "darwin" if sys.platform == "darwin" else ("win32" if os.name == "nt" else "linux")


_HAVE = {}


def have(cmd):
    """这台机器上有没有这个命令。

    ★必须走「将来真跑 RUN 的那个 shell」去查,不能用 Python 的 shutil.which:
      Windows 上 Git bash 自带的 /usr/bin 不在 Python 进程的 PATH 里,
      于是 which 说没有 bc、bash 里明明有。探针与被测必须走同一条调用路径——
      这个坑在别处已经栽过一次,这里又栽了一次。
    """
    if cmd in _HAVE:
        return _HAVE[cmd]
    try:
        r = subprocess.run([shell_path(), "-lc", "command -v " + shlex.quote(cmd)],
                           capture_output=True, timeout=60)
        ok = r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        ok = False
    _HAVE[cmd] = ok
    return ok


def plan_apt(cmd):
    """从一条 apt 命令里挑出包名，判断这台机器要不要装、装不装得上。"""
    pkgs = []
    m = re.search(r"apt-get\s+(?:-y\s+)?install\s+(?:-y\s+)?(.*)", cmd)
    if m:
        for tok in shlex.split(m.group(1).split("&&")[0]):
            if tok.startswith("-") or tok in ("apt-get", "install"):
                continue
            pkgs.append(tok)
    plat = _plat()
    todo, missing, already = [], [], []
    for p in pkgs:
        cmdname = PKG_CMD.get(p, p)
        if cmdname is None:                      # 声明为「与题目无关」
            already.append(p + "(无关)")
            continue
        if have(cmdname):                        # ★按真跑 RUN 的那个 shell 来查
            already.append(p)
            continue
        if p in PKG_NATIVE_NAME and PKG_NATIVE_NAME[p] is None:
            missing.append(p)
            continue
        recipe = INSTALLER.get(plat)
        if not recipe:
            missing.append(p)
        else:
            todo.append(recipe % p)
    return pkgs, todo, missing, already


def materialize(task_dir, root, log):
    """按 Dockerfile 在宿主机上把题目环境搭出来。"""
    steps = parse_dockerfile(task_dir / "Dockerfile")
    env = dict(os.environ)
    # Official TB base images inherit WORKDIR /app.
    base = next((step[1] for step in steps if step[0] == "FROM"), "")
    cwd = root / "app" if "ghcr.io/laude-institute/t-bench/" in base else root
    cwd.mkdir(parents=True, exist_ok=True)
    multi_stage = sum(1 for s in steps if s[0] == "FROM") > 1
    if multi_stage:
        raise Unsupported("多阶段构建，需要手工移植")

    for step in steps:
        if step[0] == "HEREDOC":
            _, dest, body = step
            f = pathlib.Path(remap(dest, root))
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(remap(body, root))
            log.append({"heredoc 写入": str(f), "字节": len(body)})
            continue
        head, rest = step
        if head in ("FROM", "LABEL", "USER", "EXPOSE", "CMD", "ENTRYPOINT", "ARG"):
            continue
        if head == "ENV":
            for kv in re.finditer(r"(\w+)=(\S+)", rest):
                env[kv.group(1)] = remap(kv.group(2), root)
            continue
        if head == "WORKDIR":
            cwd = pathlib.Path(remap(rest, root))
            cwd.mkdir(parents=True, exist_ok=True)
            continue
        if head == "COPY":
            if rest.startswith("--from"):
                # 常见写法是从官方镜像里抠一个工具二进制(uv/uvx)。本机已有同名命令就跳过；
                # 真的是多阶段构建产物才弃题。
                toks = shlex.split(rest)[1:]
                names = [pathlib.PurePosixPath(x).name for x in toks[:-1]]
                if names and all(have(n) for n in names):
                    log.append({"跳过 COPY --from(本机已有该工具)": names})
                    continue
                raise Unsupported("COPY --from 多阶段，需要手工移植：%s" % names)
            if rest.startswith("<<"):
                raise Unsupported("COPY heredoc，需要手工移植")
            parts = shlex.split(rest)
            # ★原写法末尾带斜杠 = 目标是目录。Path() 会把尾斜杠吃掉，
            #   于是「拷进 /tmp/ 目录」变成「拷成名叫 tmp 的文件」，下一步报 Not a directory。
            dst_is_dir = parts[-1].endswith("/")
            dst = pathlib.Path(remap(parts[-1], root))
            if not dst.is_absolute():
                dst = cwd / parts[-1]
            if dst_is_dir:
                dst.mkdir(parents=True, exist_ok=True)
            for src in parts[:-1]:
                s = task_dir / src.rstrip("/.")
                if src in (".", "./"):
                    s = task_dir
                if s.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                    for item in s.iterdir():
                        tgt = dst / item.name
                        if item.is_dir():
                            shutil.copytree(item, tgt, dirs_exist_ok=True)
                        else:
                            shutil.copy2(item, tgt)
                else:
                    if str(dst).endswith("/") or dst.is_dir():
                        dst.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(s, dst / s.name)
                    else:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(s, dst)
            remap_tree(dst, root, log)
            log.append({"copy": rest, "dst": str(dst)})
            continue
        if head == "RUN":
            cmd = remap(rest, root)
            if "apt-get" in cmd:
                pkgs, todo, missing, already = plan_apt(cmd)
                log.append({"apt": pkgs, "这台已有": already,
                            "这台要装": todo, "这台确实没有": missing})
                if missing:
                    raise Unsupported("这台机器没有等价物：%s" % missing)
                for t in todo:
                    run_sh(t, cwd, env, log)
                continue
            # ★pip 一律走「当前解释器 -m pip」:PATH 上的 pip 可能指着另一个
            # 甚至坏掉的解释器(本机 mac 上就指着已不存在的 Python 2.7)。
            # ★替换串一律走 lambda：re.sub 会把替换串里的反斜杠当转义处理，
            #   而 Windows 上 sys.executable 是 C:\hostedtoolcache\... ——第 3 个字符
            #   就是 \h，直接抛 "bad escape \h"。这一句对每条 RUN 都跑，
            #   于是 Windows 上 8 道题全倒在这里，症状还伪装成「移植不支持」。
            _py = shlex.quote(sys.executable)
            cmd = re.sub(r"(?<![\w/-])pip(3?) install", lambda m: _py + " -m pip install", cmd)
            head0 = cmd.strip().split()[0] if cmd.strip() else ""
            if head0 in ("locale-gen", "locale"):
                # 这两条是在 Debian 上生成 en_US.UTF-8。mac 自带，Windows 的 Git bash
                # 没有这套但也不需要。跳过即可，不必整道弃掉。
                log.append({"跳过 Linux 专属的 locale 步骤": cmd.strip()[:60]})
                continue
            if head0 == "mandb" and not have("mandb"):
                raise Unsupported("这台没有 mandb：%s" % cmd.strip()[:40])
            rc = run_sh(cmd, cwd, env, log)
            if rc != 0:
                raise Unsupported("RUN 失败 rc=%d：%s" % (rc, cmd[:80]))
            continue
    return cwd, env
