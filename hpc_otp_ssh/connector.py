#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HPC 集群连接器（默认指向北大未名一号）—— 给脚本 / agent 用的 SSH 通道

认证方式：门户密码 + OTP 动态码（keyboard-interactive）。
本工具用 hpc-otp 保存的凭据（DPAPI 加密）自动作答两次提问，
因此能做到"无人值守"地登录登录节点、提交作业、拉日志、传文件。

前置：
  pip install hpc-otp-ssh               # 或 python -m pip install paramiko
  hpc-otp import --qr 绑定页截图.png
  hpc-otp set-user 2100012345     # 学号/工号
  （密码可选：hpc-otp set-password，否则每次运行会询问）

常用命令：
  hpc-ssh check                                   # 探活 + 认证 + 打印 whoami/hostname
  hpc-ssh run "squeue -u $USER"                    # 在登录节点跑一条命令
  hpc-ssh run --json "sacct -u $USER --starttime today"   # 给 agent 解析的 JSON 输出
  hpc-ssh submit job.sh --remote-dir ~/jobs        # 上传并 sbatch
  hpc-ssh put ./data.tar ~/              --host data   # 走数据传输节点上传
  hpc-ssh get ~/job.123.out .            --host data
  hpc-ssh shell                                   # 交互式 shell（等同 ssh 登录）

注意：
  * 登录节点不可联网；需要下载/上传大文件时用 --host data（wm1-data，可联网、不能提交作业）。
  * 平台若不支持公钥登录，就只能用密码 + OTP（本工具的适用场景）。
  * 认证失败不会重试（避免触发账号封锁）。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

from . import otp as cred

# 集群地址：环境变量优先（HPC_* 为新名字，PKU_HPC_* 为兼容旧名）；
# 默认值是作者所在的北大未名一号，其它集群用环境变量覆盖即可。
LOGIN_HOST = (os.environ.get("HPC_LOGIN_HOST")
              or os.environ.get("PKU_HPC_LOGIN_HOST")
              or "wm1-login.pku.edu.cn")
DATA_HOST = (os.environ.get("HPC_DATA_HOST")
             or os.environ.get("PKU_HPC_DATA_HOST")
             or "wm1-data.pku.edu.cn")
PORT = int(os.environ.get("HPC_PORT") or os.environ.get("PKU_HPC_PORT") or 22)
KNOWN_HOSTS = Path(os.environ.get("HPC_KNOWN_HOSTS")
                   or Path.home() / ".ssh" / "hpc_otp_ssh_known_hosts")

PASSWORD_KEYS = ("password", "passwd", "passphrase", "密码", "口令")
OTP_KEYS = ("code", "otp", "verification", "token", "one-time", "动态", "令牌", "验证")


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def import_paramiko():
    try:
        import paramiko  # noqa: PLC0415
    except ImportError:
        die("缺少 paramiko，请先运行：python -m pip install paramiko", 3)
    return paramiko


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------
class Answerer:
    """按提示文本决定回密码还是回动态码。"""

    def __init__(self, password: str, secret: str, debug: bool = False):
        self.password = password
        self.secret = secret
        self.debug = debug
        self.seen = 0

    def code(self) -> str:
        return cred.make_totp(self.secret).now()

    def __call__(self, title, instructions, prompt_list):
        answers = []
        for text, _echo in prompt_list:
            low = (text or "").lower()
            is_otp = any(k in low for k in OTP_KEYS)
            is_pwd = any(k in low for k in PASSWORD_KEYS)
            if is_otp and is_pwd:
                # "One-time password:"/"动态密码:" 同时命中两边，按提问顺序判断：
                # 第一次问的是密码，之后的当动态码。
                use_otp = self.seen > 0
            elif is_otp:
                use_otp = True
            elif is_pwd:
                use_otp = False
            else:
                # 完全没见过的措辞：第一个提问给密码，后续给动态码
                use_otp = self.seen > 0
            if use_otp:
                ans, kind = self.code(), "OTP 动态码"
            else:
                ans, kind = self.password, "密码"
            self.seen += 1
            answers.append(ans)
            if self.debug:
                print(f"[auth] 服务器提问 {text!r} -> 回复 {kind}", file=sys.stderr)
        return answers


def can_prompt(force: bool = False) -> bool:
    """判断此刻是否真的有人能敲键盘。

    注意：在 agent/自动化宿主里 stdin 也可能是 tty（有伪控制台），
    那样 getpass/input 会永远等下去，所以还要看 DSH_* 这类宿主标记。
    """
    if not sys.stdin.isatty():
        return False
    if force:  # --ask-password：明确表示"我就是人，给我提示"
        return True
    return not (os.environ.get("DSH_SHELL") or os.environ.get("DSH_SESSION_ID"))


def load_credentials(args) -> tuple[str, str, str]:
    store = cred.load_store()
    username = args.user or store.get("username")
    if not username:
        die("没有上机账号。先运行：hpc-otp set-user 学号，或加 --user。")
    secret = store.get("otp_secret")
    if not secret:
        die("没有 OTP 密钥。先运行：hpc-otp import --qr 绑定页截图.png")
    password = store.get("password")
    if args.ask_password or not password:
        if not can_prompt(force=args.ask_password):
            # 故意不做空密码尝试：失败次数可能影响账号，且没有意义
            die("当前环境无法交互输入密码（agent/自动化宿主，或没有终端）。\n"
                "  办法一（推荐）：先跑一次\n"
                "      hpc-otp set-password\n"
                "  密码会用 DPAPI 加密存到本机，之后脚本/agent 都能免交互使用。\n"
                "  办法二：在真正的交互终端里运行本命令手工输入\n"
                "      （若你的终端确实是人用的却被误判，加 --ask-password 强制提示）。", 7)
        import getpass
        password = getpass.getpass(f"{username} 的门户密码（输入不显示）: ")
        if args.save_password:
            store["password"] = password
            store.setdefault("username", username)
            cred.save_store(store)
            print("密码已用 DPAPI 加密保存。", file=sys.stderr)
    return username, password, secret


def check_host_key(paramiko, transport, host: str, assume_yes: bool) -> None:
    key = transport.get_remote_server_key()
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    fp = f"SHA256:{digest}"
    known = paramiko.hostkeys.HostKeys(str(KNOWN_HOSTS)) if KNOWN_HOSTS.exists() else paramiko.hostkeys.HostKeys()
    entry = known.lookup(host)
    if entry and key.get_name() in entry:
        if entry[key.get_name()] == key:
            return
        die(f"⚠ {host} 的主机密钥与上次不同！\n  上次: {entry[key.get_name()].get_fingerprint()}\n"
            f"  本次: {fp}\n如确认平台换过密钥，请删除 {KNOWN_HOSTS} 后重连。", 4)
    print(f"首次连接 {host}，主机密钥指纹：\n  {fp}", file=sys.stderr)
    if not assume_yes:
        if not can_prompt():
            die("当前环境无法确认主机密钥，请加 --yes（或先手动连一次核对指纹）。", 4)
        if input("  接受并记住？[y/N] ").strip().lower() not in ("y", "yes"):
            die("已放弃。", 4)
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    known.add(host, key.get_name(), key)
    known.save(str(KNOWN_HOSTS))
    print(f"  已记入 {KNOWN_HOSTS}", file=sys.stderr)


def connect(args):
    paramiko = import_paramiko()
    username, password, secret = load_credentials(args)
    host = DATA_HOST if args.host == "data" else LOGIN_HOST

    transport = paramiko.Transport((host, PORT))
    transport.banner_timeout = 30
    transport.auth_timeout = 60
    try:
        transport.start_client(timeout=30)
    except Exception as exc:  # noqa: BLE001
        die(f"连不上 {host}:{PORT}（{exc}）。\n"
            f"请确认在校园网内或已连机构 VPN（HPC 集群通常不对外网开放）。", 5)

    check_host_key(paramiko, transport, host, args.yes)

    answerer = Answerer(password, secret, debug=args.debug_auth)
    try:
        transport.auth_interactive(username, answerer)
    except paramiko.BadAuthenticationType:
        try:
            transport.auth_password(username, password)
        except paramiko.AuthenticationException as exc:
            die(f"认证失败（{exc}）。不重试，请检查密码/动态码/账号。", 6)
    except paramiko.AuthenticationException as exc:
        die(f"认证失败（{exc}）。常见原因：密码错、动态码过期、账号未绑定 OTP、"
            f"时钟不准。不重试，避免触发封锁。", 6)

    if not transport.is_authenticated():
        die("认证未通过。", 6)
    transport.set_keepalive(30)
    return transport, username, host


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def run_remote(transport, cmd: str, timeout: float | None):
    chan = transport.open_session()
    chan.settimeout(0.0)
    chan.exec_command(cmd)
    out, err = bytearray(), bytearray()
    deadline = time.time() + timeout if timeout else None
    while True:
        progressed = False
        if chan.recv_ready():
            out += chan.recv(65536)
            progressed = True
        if chan.recv_stderr_ready():
            err += chan.recv_stderr(65536)
            progressed = True
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if deadline and time.time() > deadline:
            chan.close()
            return None, bytes(out), bytes(err)
        if not progressed:
            time.sleep(0.02)
    status = chan.recv_exit_status()
    chan.close()
    return status, bytes(out), bytes(err)


def cmd_check(args) -> int:
    transport, username, host = connect(args)
    try:
        status, out, err = run_remote(transport, "whoami; hostname; pwd; echo ---; "
                                                 "sinfo -s 2>&1 | head -5", 60)
        print(out.decode("utf-8", "replace"), end="")
        if err:
            print(err.decode("utf-8", "replace"), end="", file=sys.stderr)
        print(f"[已登录 {username}@{host}，退出码 {status}]", file=sys.stderr)
        return 0 if status == 0 else status
    finally:
        transport.close()


def cmd_run(args) -> int:
    transport, username, host = connect(args)
    try:
        status, out, err = run_remote(transport, args.command, args.timeout)
        out_s, err_s = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        if args.json:
            print(json.dumps({
                "host": host, "user": username, "command": args.command,
                "exit_code": status,
                "timed_out": status is None,     # 超时时命令可能仍在远端跑
                "stdout": out_s, "stderr": err_s,
            }, ensure_ascii=False))
        else:
            sys.stdout.write(out_s)
            sys.stderr.write(err_s)
        if status is None:
            if not args.json:
                print(f"[超时 {args.timeout}s，已断开该通道]", file=sys.stderr)
            return 124
        return 0 if status == 0 else status
    finally:
        transport.close()


def _sftp(transport):
    paramiko = import_paramiko()
    return paramiko.SFTPClient.from_transport(transport)


def _expand_remote(sftp, path: str) -> str:
    """把开头的 ~ 换成远端家目录。

    SFTP 协议本身不展开 ~（只有 sftp 客户端和 shell 会展开），
    所以直接把 '~/x' 交给服务器会在家目录下找名为 '~' 的目录而失败。
    """
    if path == "~" or path.startswith("~/"):
        home = sftp.normalize(".")
        rest = path[2:] if path.startswith("~/") else ""
        return f"{home}/{rest}" if rest else home
    return path


def cmd_put(args) -> int:
    transport, _u, _h = connect(args)
    try:
        sftp = _sftp(transport)
        local, remote = Path(args.local), args.remote
        if remote.endswith("/"):
            remote = remote + local.name
        remote = _expand_remote(sftp, remote)
        sftp.put(str(local), remote)
        size = sftp.stat(remote).st_size
        print(f"已上传 {local} -> {remote}（{size} 字节）")
        return 0
    finally:
        transport.close()


def cmd_get(args) -> int:
    transport, _u, _h = connect(args)
    try:
        sftp = _sftp(transport)
        target = Path(args.local)
        if target.is_dir():
            target = target / Path(args.remote).name
        sftp.get(_expand_remote(sftp, args.remote), str(target))
        print(f"已下载 {args.remote} -> {target}（{target.stat().st_size} 字节）")
        return 0
    finally:
        transport.close()


def normalize_script_bytes(path: Path) -> tuple[bytes, bool]:
    """把 Windows 换行(CRLF)的作业脚本转成 LF。

    在 Windows 上编辑的脚本直接 scp 到 Linux 集群，会报
    ``/bin/bash^M: bad interpreter``（或 sbatch 莫名其妙的语法错），
    所以提交前统一换行符。二进制文件原样返回。
    """
    raw = path.read_bytes()
    if b"\x00" in raw:                 # 看着像二进制，别动
        return raw, False
    fixed = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return fixed, fixed != raw


def cmd_submit(args) -> int:
    transport, _u, _h = connect(args)
    try:
        sftp = _sftp(transport)
        script = Path(args.script)
        remote_dir = args.remote_dir.rstrip("/")
        remote = _expand_remote(sftp, f"{remote_dir}/{script.name}")
        data, converted = normalize_script_bytes(script)
        if converted:
            print("[提示] 脚本是 CRLF 换行，已按 LF 上传（避免 Linux 上的 ^M 报错）")
        sftp.putfo(io.BytesIO(data), remote)
        status, out, err = run_remote(transport, f"cd {remote_dir} && sbatch {script.name}", 120)
        sys.stdout.write(out.decode("utf-8", "replace"))
        sys.stderr.write(err.decode("utf-8", "replace"))
        if status != 0:
            return status or 1
        # 顺手把作业号解析出来
        for line in out.decode("utf-8", "replace").splitlines():
            if "Submitted batch job" in line:
                jobid = line.split()[-1]
                print(f"[已提交作业 {jobid}]  跟踪：hpc-ssh run \"squeue -j {jobid}\"")
                break
        return 0
    finally:
        transport.close()


KEYMAP = {"H": b"\x1b[A", "P": b"\x1b[B", "M": b"\x1b[C", "K": b"\x1b[D",
          "S": b"\x1b[3~", "G": b"\x1b[H", "O": b"\x1b[F"}


def cmd_shell(args) -> int:
    import msvcrt
    transport, username, host = connect(args)
    try:
        chan = transport.open_session()
        chan.get_pty(term="xterm-256color", width=120, height=32)
        chan.invoke_shell()
        chan.settimeout(0.0)
        print(f"[已登录 {username}@{host}；Ctrl+D 或输入 exit 退出]", file=sys.stderr)
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    ch = KEYMAP.get(msvcrt.getwch(), b"")
                    if ch:
                        chan.send(ch)
                elif ch == "\r":
                    chan.send(b"\r")
                elif ch == "\x03":
                    chan.send(b"\x03")
                else:
                    chan.send(ch.encode("utf-8", "replace"))
            try:
                data = chan.recv(65536)
                if data:
                    sys.stdout.write(data.decode("utf-8", "replace"))
                    sys.stdout.flush()
                elif chan.exit_status_ready():
                    break
            except Exception:  # noqa: BLE001
                if chan.exit_status_ready():
                    break
            time.sleep(0.01)
        print(f"\n[会话结束，退出码 {chan.recv_exit_status()}]", file=sys.stderr)
        return 0
    finally:
        transport.close()


def build_parser() -> argparse.ArgumentParser:
    # 全局参数同时挂在主解析器和各子命令上（default=SUPPRESS，避免子命令把主解析器的值覆盖掉），
    # 这样 `wm1.py --user X run ...` 和 `wm1.py run ... --user X` 两种写法都能用。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--user", default=argparse.SUPPRESS,
                        help="上机账号（学号/工号），默认取本地保存值")
    common.add_argument("--host", choices=["login", "data"], default=argparse.SUPPRESS,
                        help="login=wm1-login（提交作业），data=wm1-data（传文件、可联网）")
    common.add_argument("--ask-password", action="store_true", default=argparse.SUPPRESS,
                        help="强制本次手工输入密码")
    common.add_argument("--save-password", action="store_true", default=argparse.SUPPRESS,
                        help="把本次输入的密码 DPAPI 加密保存")
    common.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                        help="首次连接自动接受主机密钥")
    common.add_argument("--debug-auth", action="store_true", default=argparse.SUPPRESS,
                        help="打印服务器提问原文（排查认证问题用）")

    p = argparse.ArgumentParser(
        parents=[common],
        description="未名一号连接器（密码 + OTP 自动登录，供脚本/agent 使用）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", parents=[common], help="探活：认证 + 打印 whoami/hostname/sinfo")

    r = sub.add_parser("run", parents=[common], help="在远端执行一条命令")
    r.add_argument("command")
    r.add_argument("--timeout", type=float, default=120, help="秒，默认 120")
    r.add_argument("--json", action="store_true", help="以 JSON 输出，便于 agent 解析")

    s = sub.add_parser("submit", parents=[common], help="上传脚本并 sbatch")
    s.add_argument("script")
    s.add_argument("--remote-dir", default="~", help="远端目录，默认 ~")

    pu = sub.add_parser("put", parents=[common], help="上传文件（建议配 --host data）")
    pu.add_argument("local")
    pu.add_argument("remote")

    ge = sub.add_parser("get", parents=[common], help="下载文件（建议配 --host data）")
    ge.add_argument("remote")
    ge.add_argument("local")

    sub.add_parser("shell", parents=[common], help="交互式 shell（等同 ssh 登录）")
    return p


def _fill_defaults(args):
    for name, default in (("user", None), ("host", "login"), ("yes", False),
                          ("debug_auth", False), ("ask_password", False),
                          ("save_password", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def main(argv=None) -> int:
    args = _fill_defaults(build_parser().parse_args(argv))
    return {
        "check": cmd_check,
        "run": cmd_run,
        "submit": cmd_submit,
        "put": cmd_put,
        "get": cmd_get,
        "shell": cmd_shell,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
