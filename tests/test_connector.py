# -*- coding: utf-8 -*-
"""hpc_otp_ssh.connector 的单元测试。

全部离线：用桩对象替换 paramiko 的 Transport/Channel，不连真实集群。
"""
import argparse
import importlib
import io
import json
import stat as stat_mod
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_otp_ssh import connector as ssh


# --------------------------------------------------------------------------
# 桩：伪造 paramiko 的 Channel / Transport
# --------------------------------------------------------------------------
class FakeChannel:
    def __init__(self, out=b"", err=b"", status=0, hang=False):
        self._out = [out[i:i + 7] for i in range(0, len(out), 7)]
        self._err = [err[i:i + 5] for i in range(0, len(err), 5)]
        self.status = status
        self.hang = hang
        self.command = None
        self.closed = False

    def settimeout(self, _t):
        pass

    def exec_command(self, cmd):
        self.command = cmd

    def recv_ready(self):
        return bool(self._out)

    def recv(self, _n):
        return self._out.pop(0)

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv_stderr(self, _n):
        return self._err.pop(0)

    def exit_status_ready(self):
        return False if self.hang else not self._out and not self._err

    def recv_exit_status(self):
        return self.status

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, channel):
        self.channel = channel

    def open_session(self):
        return self.channel

    def close(self):
        pass


def make_args(**kw):
    base = dict(command="squeue -u me", timeout=5, json=True, user=None, host="login",
                ask_password=False, save_password=False, yes=True, debug_auth=False)
    base.update(kw)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
# 认证作答：把服务器提问正确映射成 密码 / 动态码
# --------------------------------------------------------------------------
@pytest.mark.parametrize("prompts,expected", [
    (["Password: ", "Verification Code: "], ["pwd", "otp"]),
    (["Password: ", "One-time password: "], ["pwd", "otp"]),
    (["password: ", "otp: "], ["pwd", "otp"]),
    (["密码: ", "动态密码: "], ["pwd", "otp"]),
    (["Please enter your passphrase: "], ["pwd"]),
    (["Verification code: "], ["otp"]),
    (["Enter code: "], ["otp"]),
    (["", ""], ["pwd", "otp"]),          # 完全没有提示文本时按顺序兜底
    (["One-time password: "], ["pwd"]),  # 文档化的歧义：单个歧义提问按"第一问=密码"
])
def test_answerer_maps_prompts(prompts, expected):
    answerer = ssh.Answerer("MyP@ssw0rd", "JBSWY3DPEHPK3PXP")
    answers = answerer("", "", [(p, False) for p in prompts])
    kinds = ["pwd" if a == "MyP@ssw0rd" else ("otp" if a.isdigit() and len(a) == 6 else "?")
             for a in answers]
    assert kinds == expected


def test_answerer_uses_fresh_code():
    answerer = ssh.Answerer("pw", "JBSWY3DPEHPK3PXP")
    answers = answerer("", "", [("Verification Code: ", False), ("Verification Code: ", False)])
    assert answers[0] == answers[1] == ssh.cred.make_totp("JBSWY3DPEHPK3PXP").now()


# --------------------------------------------------------------------------
# 远端命令执行
# --------------------------------------------------------------------------
def test_run_remote_collects_and_returns_status():
    chan = FakeChannel(b"JOBID PARTITION\n1 C032M0128G\n", b"warning\n", status=7)
    status, out, err = ssh.run_remote(FakeTransport(chan), "squeue -u me", 5)
    assert status == 7
    assert out.decode() == "JOBID PARTITION\n1 C032M0128G\n"
    assert err.decode() == "warning\n"
    assert chan.command == "squeue -u me"
    assert chan.closed


def test_run_remote_timeout_returns_none():
    chan = FakeChannel(hang=True)
    started = time.time()
    status, _out, _err = ssh.run_remote(FakeTransport(chan), "sleep 999", 0.3)
    assert status is None
    assert time.time() - started < 5
    assert chan.closed


# --------------------------------------------------------------------------
# cmd_run 的对外契约（agent 依赖它）
# --------------------------------------------------------------------------
def _run_capture(monkeypatch, channel, **kw):
    monkeypatch.setattr(ssh, "connect", lambda _a: (FakeTransport(channel), "u", "h"))
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        rc = ssh.cmd_run(make_args(**kw))
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def test_cmd_run_json_success(monkeypatch):
    rc, out = _run_capture(monkeypatch, FakeChannel(b"ok\n", b"", status=0))
    payload = json.loads(out)
    assert rc == 0
    assert payload["exit_code"] == 0
    assert payload["timed_out"] is False
    assert payload["stdout"] == "ok\n"
    assert set(payload) == {"host", "user", "command", "exit_code", "timed_out",
                            "stdout", "stderr"}


@pytest.mark.parametrize("remote_status", [1, 7, 127, 255])
def test_cmd_run_passes_through_remote_exit_code(monkeypatch, remote_status):
    rc, _out = _run_capture(monkeypatch, FakeChannel(b"", b"boom\n", status=remote_status))
    assert rc == remote_status


def test_cmd_run_timeout_contract(monkeypatch):
    """超时：exit_code=None、timed_out=True、本地退出码 124。"""
    rc, out = _run_capture(monkeypatch, FakeChannel(hang=True), timeout=0.3)
    payload = json.loads(out)
    assert rc == 124
    assert payload["timed_out"] is True
    assert payload["exit_code"] is None


# --------------------------------------------------------------------------
# SFTP 远端路径
# --------------------------------------------------------------------------
class FakeSFTP:
    def normalize(self, _path):
        return "/gpfs/share/home/2100012345"


@pytest.mark.parametrize("raw,expected", [
    ("~/job.sh", "/gpfs/share/home/2100012345/job.sh"),
    ("~", "/gpfs/share/home/2100012345"),
    ("~/sub/dir/x", "/gpfs/share/home/2100012345/sub/dir/x"),
    ("/abs/path", "/abs/path"),
    ("relative.txt", "relative.txt"),
])
def test_expand_remote_handles_tilde(raw, expected):
    """SFTP 协议不展开 ~（只有 shell 和 sftp 客户端会），必须自己换掉。"""
    assert ssh._expand_remote(FakeSFTP(), raw) == expected


# --------------------------------------------------------------------------
# 交互性判断：agent 宿主里绝不能死等输入
# --------------------------------------------------------------------------
class FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.mark.parametrize("tty,dsh,expected", [
    (False, False, False),
    (True, False, True),
    (True, True, False),     # 伪终端但宿主是 agent：不能提示
    (False, True, False),
])
def test_can_prompt(monkeypatch, tty, dsh, expected):
    monkeypatch.setattr(ssh.sys, "stdin", FakeStdin(tty))
    if dsh:
        monkeypatch.setenv("DSH_SHELL", "1")
    else:
        monkeypatch.delenv("DSH_SHELL", raising=False)
        monkeypatch.delenv("DSH_SESSION_ID", raising=False)
    assert ssh.can_prompt() is expected


def test_can_prompt_force_overrides_agent_detection(monkeypatch):
    monkeypatch.setattr(ssh.sys, "stdin", FakeStdin(True))
    monkeypatch.setenv("DSH_SHELL", "1")
    assert ssh.can_prompt(force=True) is True


# --------------------------------------------------------------------------
# 参数解析：全局参数放在子命令前后都要能用
# --------------------------------------------------------------------------
def test_global_flags_work_before_and_after_subcommand():
    parser = ssh.build_parser()
    before = ssh._fill_defaults(parser.parse_args(["--user", "u1", "run", "echo hi"]))
    after = ssh._fill_defaults(parser.parse_args(["run", "echo hi", "--user", "u2"]))
    assert before.user == "u1"
    assert after.user == "u2"
    assert after.command == "echo hi"
    assert after.host == "login"          # 未指定时应有默认值
    assert after.timeout == 120


def test_host_flag_accepts_data():
    parser = ssh.build_parser()
    args = ssh._fill_defaults(parser.parse_args(["put", "a", "b", "--host", "data"]))
    assert args.host == "data"


def test_env_var_overrides_cluster_hosts(monkeypatch):
    """集群地址可以用 HPC_* 环境变量覆盖（不写死某一所学校）。"""
    monkeypatch.setenv("HPC_LOGIN_HOST", "login.example.org")
    monkeypatch.setenv("HPC_DATA_HOST", "data.example.org")
    monkeypatch.setenv("HPC_PORT", "2222")
    module = importlib.reload(ssh)
    try:
        assert module.LOGIN_HOST == "login.example.org"
        assert module.DATA_HOST == "data.example.org"
        assert module.PORT == 2222
    finally:
        monkeypatch.undo()
        importlib.reload(module)


# --------------------------------------------------------------------------
# 目录递归传输（scp -r 语义）：上传分析脚本目录时必需
# --------------------------------------------------------------------------
class FakeSFTPTree:
    """内存版 SFTP，用来验证 _mkdir_p / _put_tree / _get_tree 与 put/get 的目录语义。"""

    def __init__(self, dirs=(), files=None):
        self.dirs = set(dirs)
        self.files = dict(files or {})

    def normalize(self, _path):
        return "/home/u"

    def stat(self, path):
        if path in self.dirs:
            return SimpleNamespace(st_mode=stat_mod.S_IFDIR)
        if path in self.files:
            return SimpleNamespace(st_mode=stat_mod.S_IFREG, st_size=len(self.files[path]))
        raise IOError(f"no such file: {path}")

    def mkdir(self, path):
        if path in self.dirs:
            raise IOError("already exists")
        self.dirs.add(path)

    def put(self, local, remote):
        self.files[remote] = Path(local).read_bytes()

    def get(self, remote, local):
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[remote])

    def listdir_attr(self, path):
        prefix = path.rstrip("/") + "/"
        seen, out = set(), []
        for collection, mode in ((self.dirs, stat_mod.S_IFDIR),
                                 (self.files, stat_mod.S_IFREG)):
            for item in collection:
                if not item.startswith(prefix):
                    continue
                name = item[len(prefix):]
                if not name or "/" in name or name in seen:
                    continue
                seen.add(name)
                out.append(SimpleNamespace(filename=name, st_mode=mode))
        return out


def test_mkdir_p_creates_each_level():
    sftp = FakeSFTPTree()
    ssh._mkdir_p(sftp, "/home/u/a/b/c")
    assert sftp.dirs == {"/home", "/home/u", "/home/u/a", "/home/u/a/b", "/home/u/a/b/c"}


def test_mkdir_p_ignores_existing_levels():
    sftp = FakeSFTPTree(dirs={"/home", "/home/u"})
    ssh._mkdir_p(sftp, "/home/u/x/y")
    assert {"/home/u/x", "/home/u/x/y"} <= sftp.dirs


def test_put_tree_uploads_nested_and_skips_junk(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_bytes(b"print(1)")
    (tmp_path / "sub" / "b.sh").write_bytes(b"#!/bin/bash\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_bytes(b"junk")

    sftp = FakeSFTPTree()
    files, total = ssh._put_tree(sftp, tmp_path, "/home/u/dst")

    assert files == 2                                   # __pycache__ 与 .git 被跳过
    assert set(sftp.files) == {"/home/u/dst/a.py", "/home/u/dst/sub/b.sh"}
    assert total == len(b"print(1)") + len(b"#!/bin/bash\n")
    assert "/home/u/dst/sub" in sftp.dirs


def test_get_tree_downloads_nested(tmp_path):
    sftp = FakeSFTPTree(dirs={"/r", "/r/sub"},
                        files={"/r/x.csv": b"a,b\n", "/r/sub/y.py": b"print(1)\n"})
    files, total = ssh._get_tree(sftp, "/r", tmp_path / "out")

    assert files == 2
    assert total == len(b"a,b\n") + len(b"print(1)\n")
    assert (tmp_path / "out" / "x.csv").read_bytes() == b"a,b\n"
    assert (tmp_path / "out" / "sub" / "y.py").read_bytes() == b"print(1)\n"


def test_cmd_put_directory_drops_into_target(monkeypatch, tmp_path, capsys):
    """put 目录到以 / 结尾的目标 -> 在目标下建同名子目录（与 scp -r 一致）。"""
    src = tmp_path / "8_analysis"
    src.mkdir()
    (src / "a.py").write_text("x")
    sftp = FakeSFTPTree()
    monkeypatch.setattr(ssh, "connect", lambda _a: (FakeTransport(FakeChannel()), "u", "h"))
    monkeypatch.setattr(ssh, "_sftp", lambda _t: sftp)

    args = make_args(local=str(src), remote="~/vns/AB_run/")
    assert ssh.cmd_put(args) == 0

    assert "/home/u/vns/AB_run/8_analysis/a.py" in sftp.files
    assert "已上传目录" in capsys.readouterr().out


def test_cmd_put_single_file_creates_parent_dir(monkeypatch, tmp_path, capsys):
    src = tmp_path / "job.sh"
    src.write_bytes(b"#!/bin/bash\n")     # 用字节写入，避免 Windows 把 \n 变成 \r\n
    sftp = FakeSFTPTree()
    monkeypatch.setattr(ssh, "connect", lambda _a: (FakeTransport(FakeChannel()), "u", "h"))
    monkeypatch.setattr(ssh, "_sftp", lambda _t: sftp)

    args = make_args(local=str(src), remote="~/jobs/job.sh")
    assert ssh.cmd_put(args) == 0

    assert sftp.files["/home/u/jobs/job.sh"] == b"#!/bin/bash\n"
    assert "/home/u/jobs" in sftp.dirs            # 远端父目录自动创建


def test_cmd_put_missing_local_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh, "connect", lambda _a: (FakeTransport(FakeChannel()), "u", "h"))
    monkeypatch.setattr(ssh, "_sftp", lambda _t: FakeSFTPTree())
    with pytest.raises(SystemExit):
        ssh.cmd_put(make_args(local=str(tmp_path / "nope"), remote="~/x"))


def test_cmd_get_directory_creates_named_subdir(monkeypatch, tmp_path, capsys):
    sftp = FakeSFTPTree(dirs={"/home/u/8_kslice"},
                        files={"/home/u/8_kslice/wannier90-kslice.dat": b"1 2 3\n"})
    monkeypatch.setattr(ssh, "connect", lambda _a: (FakeTransport(FakeChannel()), "u", "h"))
    monkeypatch.setattr(ssh, "_sftp", lambda _t: sftp)

    args = make_args(remote="~/8_kslice", local=str(tmp_path))
    assert ssh.cmd_get(args) == 0

    assert (tmp_path / "8_kslice" / "wannier90-kslice.dat").read_bytes() == b"1 2 3\n"
    assert "已下载目录" in capsys.readouterr().out


def test_cmd_get_missing_remote_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh, "connect", lambda _a: (FakeTransport(FakeChannel()), "u", "h"))
    monkeypatch.setattr(ssh, "_sftp", lambda _t: FakeSFTPTree())
    with pytest.raises(SystemExit):
        ssh.cmd_get(make_args(remote="~/nope", local=str(tmp_path)))


# --------------------------------------------------------------------------
# 作业脚本换行符：Windows 编辑的脚本直接传上 Linux 会报 /bin/bash^M
# --------------------------------------------------------------------------
def test_normalize_script_bytes_converts_crlf(tmp_path):
    p = tmp_path / "job.sh"
    p.write_bytes(b"#!/bin/bash\r\n#SBATCH -J x\r\nhostname\r\n")
    data, changed = ssh.normalize_script_bytes(p)
    assert changed is True
    assert data == b"#!/bin/bash\n#SBATCH -J x\nhostname\n"


def test_normalize_script_bytes_leaves_lf_alone(tmp_path):
    p = tmp_path / "job.sh"
    p.write_bytes(b"#!/bin/bash\nhostname\n")
    data, changed = ssh.normalize_script_bytes(p)
    assert changed is False
    assert data == b"#!/bin/bash\nhostname\n"


def test_normalize_script_bytes_skips_binary(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x00\x01\r\n\x02")
    data, changed = ssh.normalize_script_bytes(p)
    assert changed is False
    assert data == b"\x00\x01\r\n\x02"


class FakeSFTPPut:
    """只记录 putfo 收到的内容，用来验证提交前的换行符转换。"""

    def __init__(self):
        self.uploaded = None
        self.remote = None

    def normalize(self, _path):
        return "/home/u"

    def putfo(self, fl, remote):
        self.uploaded = fl.read()
        self.remote = remote


def test_cmd_submit_converts_crlf_and_reports_jobid(monkeypatch, tmp_path, capsys):
    script = tmp_path / "job.sh"
    script.write_bytes(b"#!/bin/bash\r\nhostname\r\n")
    fake_sftp = FakeSFTPPut()
    chan = FakeChannel(b"Submitted batch job 4242\n", b"", status=0)
    monkeypatch.setattr(ssh, "connect", lambda _a: (FakeTransport(chan), "u", "h"))
    monkeypatch.setattr(ssh, "_sftp", lambda _t: fake_sftp)

    args = make_args(script=str(script), remote_dir="~/jobs")
    assert ssh.cmd_submit(args) == 0

    out = capsys.readouterr().out
    assert fake_sftp.uploaded == b"#!/bin/bash\nhostname\n"   # CRLF 已转 LF
    assert fake_sftp.remote == "/home/u/jobs/job.sh"          # ~ 已展开
    assert "CRLF" in out
    assert "4242" in out
