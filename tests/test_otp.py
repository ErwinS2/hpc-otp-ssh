# -*- coding: utf-8 -*-
"""hpc_otp_ssh.otp 的单元测试：TOTP 计算、密钥解析、打码、凭据存储。"""
import argparse
import os
from pathlib import Path

import pytest
import pyotp

from hpc_otp_ssh import otp

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="凭据后端目前仅 Windows DPAPI")


# --------------------------------------------------------------------------
# TOTP
# --------------------------------------------------------------------------
def test_rfc6238_standard_vector():
    """标准向量能对上，说明依赖与时钟算法没被改坏。"""
    assert pyotp.TOTP("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", digits=8).at(59) == "94287082"


def test_normalize_secret_strips_noise():
    assert otp.normalize_secret(" jbsw y3dp-ehpk 3pxp= ") == "JBSWY3DPEHPK3PXP"


@pytest.mark.parametrize("raw,expected", [
    ("ABCDEF", "ABCDEF=="),                      # 6 -> 补到 8（注意这仍是非法 Base32 长度）
    ("JBSWY3DPEHPK3PXP", "JBSWY3DPEHPK3PXP"),    # 已是 8 的倍数
    ("abcde", "ABCDE==="),                       # 5 -> 补 3 位，合法长度
])
def test_pad_secret(raw, expected):
    """Base32 长度必须是 8 的倍数，否则 pyotp 抛 Incorrect padding。"""
    assert otp.pad_secret(raw) == expected


def test_valid_odd_length_secret_computes():
    """5 位（补 3 个 = ）是合法 Base32，能算出来。"""
    assert len(otp.make_totp("ABCDE").now()) == 6


def test_invalid_base32_length_raises():
    """6 位是非法 Base32 长度（补 2 个 = 也不合法），必须报错，不能静默算错。"""
    with pytest.raises(Exception):
        otp.make_totp("ABCDEF").now()


# --------------------------------------------------------------------------
# 二维码 / otpauth 解析
# --------------------------------------------------------------------------
@pytest.mark.parametrize("payload,secret,label", [
    # 真实平台形态：path 里没有冒号，整段就是标签
    ("otpauth://totp/2100012345@pkuhpc?secret=JBSWY3DPEHPK3PXP&issuer=pkuhpc",
     "JBSWY3DPEHPK3PXP", "2100012345@pkuhpc"),
    # 标准形态：issuer:account
    ("otpauth://totp/pkuhpc:2100012345?secret=JBSWY3DPEHPK3PXP&issuer=pkuhpc",
     "JBSWY3DPEHPK3PXP", "2100012345"),
    # 只有查询串
    ("secret=JBSWY3DPEHPK3PXP&issuer=x", "JBSWY3DPEHPK3PXP", ""),
    # 裸密钥
    ("JBSWY3DPEHPK3PXP", "JBSWY3DPEHPK3PXP", ""),
])
def test_secret_from_payload(payload, secret, label):
    assert otp.secret_from_payload(payload) == (secret, label)


@pytest.mark.parametrize("label,expected", [
    ("2100012345@pkuhpc", "2100012345"),
    ("2100012345", "2100012345"),
    ("MyToken", ""),
    ("", ""),
    ("12345", ""),        # 太短，不像账号
    ("1234567890123456", ""),  # 太长
])
def test_account_from_label(label, expected):
    assert otp.account_from_label(label) == expected


# --------------------------------------------------------------------------
# 打码：输出里不应出现明文密钥/账号
# --------------------------------------------------------------------------
def test_mask_secret_hides_value():
    secret = "JBSWY3DPEHPK3PXP"
    shown = otp.mask_secret(f"otpauth://totp/x?secret={secret}", secret, show=False)
    assert secret not in shown
    assert "JBSW****3PXP" in shown


def test_mask_secret_respects_show_flag_and_case():
    secret = "JBSWY3DPEHPK3PXP"
    assert otp.mask_secret("whatever", secret, show=True) == "whatever"
    # URI 里是小写也要能打掉
    assert secret.lower() not in otp.mask_secret(secret.lower(), secret, show=False)


def test_mask_accountish_only_masks_digit_labels():
    assert otp.mask_accountish("2100012345@pkuhpc") == "2100****45@pkuhpc"
    assert otp.mask_accountish("MyToken") == "MyToken"
    assert otp.mask_accountish("") == ""


# --------------------------------------------------------------------------
# 凭据存储
# --------------------------------------------------------------------------
@WINDOWS_ONLY
def test_store_roundtrip(temp_store):
    assert otp.load_store() == {}
    otp.save_store({"username": "2100012345", "otp_secret": "JBSWY3DPEHPK3PXP"})
    assert temp_store.exists()
    assert temp_store.read_bytes().startswith(otp.MAGIC)

    store = otp.load_store()
    assert store["username"] == "2100012345"
    assert otp.current_code(store).isdigit()


@WINDOWS_ONLY
def test_store_file_is_encrypted(temp_store):
    """明文不得落盘：文件里不应出现密钥/密码字样。"""
    otp.save_store({"otp_secret": "JBSWY3DPEHPK3PXP", "password": "SuperSecret123!"})
    raw = temp_store.read_bytes()
    assert b"JBSWY3DPEHPK3PXP" not in raw
    assert b"SuperSecret123!" not in raw


@WINDOWS_ONLY
def test_legacy_magic_is_still_readable(temp_store):
    """旧版本(0.0.x)写出的 PKUHPC1 格式必须继续可读，否则用户凭据会失效。"""
    otp.save_store({"username": "legacy-user"})
    raw = temp_store.read_bytes()
    legacy = otp.LEGACY_MAGICS[0] + raw[len(otp.MAGIC):]
    temp_store.write_bytes(legacy)

    assert temp_store.read_bytes().startswith(otp.LEGACY_MAGICS[0])
    assert otp.load_store()["username"] == "legacy-user"


@WINDOWS_ONLY
def test_legacy_entropy_is_still_readable(temp_store):
    """旧版本用过不同的 DPAPI 熵值，读取时必须回退兼容，否则用户凭据失联。"""
    import json
    payload = json.dumps({"username": "legacy-entropy"}).encode("utf-8")
    blob = otp.MAGIC + otp._dpapi(payload, protect=True,
                                  entropy=otp.LEGACY_ENTROPIES[0])
    temp_store.write_bytes(blob)
    assert otp.load_store()["username"] == "legacy-entropy"


@WINDOWS_ONLY
def test_wrong_entropy_gives_clear_error(temp_store):
    """用完全对不上的熵值加密 -> 应报"解不开"而不是抛 OSError。"""
    blob = otp.MAGIC + otp._dpapi(b'{"a":1}', protect=True, entropy=b"totally-wrong")
    temp_store.write_bytes(blob)
    with pytest.raises(RuntimeError, match="解不开"):
        otp.load_store()


def test_foreign_file_is_rejected(temp_store):
    temp_store.write_bytes(b"this is not our file")
    with pytest.raises(RuntimeError, match="不是本工具生成的文件"):
        otp.load_store()


def test_wintypes_import_is_platform_guarded():
    """回归保护：ctypes.wintypes 在 Linux/macOS 上 import 就抛错。

    它必须待在 ``if os.name == "nt":`` 分支里，否则非 Windows 用户
    连一条友好报错都看不到，直接 ImportError/ValueError 崩在 import。
    （真实行为由 CI 的 ubuntu job 验证——本机无法伪造成 Linux，
    因为 pathlib 也依赖 os.name。）
    """
    import ast

    tree = ast.parse(Path(otp.__file__).read_text(encoding="utf-8"))
    for node in tree.body:  # 只看模块顶层语句
        if isinstance(node, ast.ImportFrom) and node.module == "ctypes":
            names = [alias.name for alias in node.names]
            assert "wintypes" not in names, (
                "ctypes.wintypes 不能出现在模块顶层：Linux 上 import 即报错")
    source = Path(otp.__file__).read_text(encoding="utf-8")
    assert 'if os.name == "nt":' in source, "平台分支不见了"


def test_current_code_without_secret_errors(temp_store):
    with pytest.raises(SystemExit):
        otp.current_code({})


@WINDOWS_ONLY
def test_import_rejects_unusable_secret(temp_store, capsys):
    """坏密钥不能落盘：先验证再保存。"""
    args = argparse.Namespace(qr=None, uri=None, secret="ABCDEF", label=None,
                              show_secret=False)
    rc = otp.cmd_import(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert not temp_store.exists(), "非法密钥不应写入凭据文件"
    assert "算不出动态码" in out


@WINDOWS_ONLY
def test_import_accepts_valid_secret_and_records_account(temp_store, capsys):
    args = argparse.Namespace(
        qr=None, uri="otpauth://totp/2100012345@example?secret=JBSWY3DPEHPK3PXP&issuer=example",
        secret=None, label=None, show_secret=False)
    assert otp.cmd_import(args) == 0
    out = capsys.readouterr().out
    assert "已保存" in out
    store = otp.load_store()
    assert store["username"] == "2100012345"
    assert store["otp_secret"] == "JBSWY3DPEHPK3PXP"
    # 明文不应出现在输出里
    assert "JBSWY3DPEHPK3PXP" not in out
    assert "2100012345" not in out


@WINDOWS_ONLY
def test_default_store_path_prefers_existing_repo_file(monkeypatch):
    """默认路径应能定位到已存在的 cred.dat（本例即仓库根目录的那个）。"""
    monkeypatch.delenv("HPC_OTP_STORE", raising=False)
    monkeypatch.delenv("PKU_HPC_STORE", raising=False)
    path = otp.default_store_path()
    assert path.name == "cred.dat"


def test_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "custom.dat"
    monkeypatch.setenv("HPC_OTP_STORE", str(target))
    assert otp.default_store_path() == target


def test_legacy_env_override_still_works(monkeypatch, tmp_path):
    target = tmp_path / "legacy.dat"
    monkeypatch.delenv("HPC_OTP_STORE", raising=False)
    monkeypatch.setenv("PKU_HPC_STORE", str(target))
    assert otp.default_store_path() == target
