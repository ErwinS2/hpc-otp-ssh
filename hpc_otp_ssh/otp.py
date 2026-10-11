#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HPC 平台 OTP 密钥 / 凭据管理工具（密码 + TOTP 双因素）

作用：把绑定页二维码里的 TOTP 密钥用 Windows DPAPI 加密存在本机，
      需要时生成 6 位动态码，供 ssh 或自动化脚本使用。

安全提醒：
  * 密钥和密码都用 Windows DPAPI 加密，只有当前 Windows 用户能解开；
    （Linux/macOS 的凭据后端尚未实现，见 README『已知限制』）
  * 这份文件与 cred.dat 不要放进 git / 网盘 / 任何同步目录；
  * 把 OTP 密钥导出到电脑等于把第二因素降级为"本机文件"，
    是否允许脚本化登录，请先与所在集群的管理方确认。

常用命令：
  hpc-otp selftest                     # 自检，不需要账号
  hpc-otp import --qr 绑定页截图.png   # 从二维码截图导入密钥
  hpc-otp import --secret JBSWY3DPEHPK3PXP
  hpc-otp set-user 2100012345          # 记住上机账号（学号/工号）
  hpc-otp set-password                 # 记住门户密码（可选）
  hpc-otp verify                       # 循环显示动态码，去网页点"验证手机令牌"
  hpc-otp code                         # 只打印当前动态码
  hpc-otp show                         # 查看已存内容（掩码显示）
  hpc-otp clear                        # 删除本地凭据
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path

MAGIC = b"HPCOPT1\x00"
LEGACY_MAGICS = (b"PKUHPC1\x00",)   # 兼容旧版本写出的凭据文件
ENTROPY = b"hpc-otp-ssh"            # DPAPI 的附加熵（会参与密钥派生）
LEGACY_ENTROPIES = (b"pku-hpc-otp",)  # 旧版本用过的熵，读取时回退尝试


def default_store_path() -> Path:
    """按顺序挑一个凭据文件路径。

    1. 环境变量 ``HPC_OTP_STORE``（兼容旧的 ``PKU_HPC_STORE``）
    2. 仓库根目录下的 cred.dat（源码运行时最顺手）
    3. 包目录下的 cred.dat
    4. ``%LOCALAPPDATA%/hpc-otp-ssh/cred.dat``（pip 安装后的默认位置）
    """
    env = os.environ.get("HPC_OTP_STORE") or os.environ.get("PKU_HPC_STORE")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "cred.dat",
        here.parent / "cred.dat",
        Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "hpc-otp-ssh" / "cred.dat",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


STORE_PATH = default_store_path()


# --------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------
# 注意：ctypes.wintypes 在 Linux/macOS 上 import 就会抛错，
# 所以整块必须在 os.name 分支里定义，保证非 Windows 上也能正常 import 本模块
# （然后由 _dpapi 给出清晰的"后端未实现"报错，而不是崩在 import）。
if os.name == "nt":
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]
else:  # pragma: no cover - 平台分支
    _DataBlob = None


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool, entropy: bytes | None = None) -> bytes:
    """用 DPAPI 加/解密（绑定当前 Windows 用户）。"""
    if os.name != "nt":
        raise RuntimeError(
            "凭据加密目前依赖 Windows DPAPI；Linux/macOS 后端尚未实现，见 README『已知限制』")
    entropy = ENTROPY if entropy is None else entropy
    crypt32 = ctypes.windll.crypt32
    blob_in, _keep1 = _blob(data)
    blob_ent, _keep2 = _blob(entropy)
    blob_out = _DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI 调用失败，错误码 {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------
# 凭据存储
# --------------------------------------------------------------------------
def load_store() -> dict:
    if not STORE_PATH.exists():
        return {}
    raw = STORE_PATH.read_bytes()
    for magic in (MAGIC, *LEGACY_MAGICS):
        if not raw.startswith(magic):
            continue
        payload = raw[len(magic):]
        for entropy in (ENTROPY, *LEGACY_ENTROPIES):
            try:
                return json.loads(_dpapi(payload, protect=False, entropy=entropy)
                                  .decode("utf-8"))
            except OSError:
                continue      # 熵值不匹配，试下一个（用于兼容旧版本写出的凭据）
        raise RuntimeError(
            f"{STORE_PATH} 解不开：DPAPI 只允许加密它的那个 Windows 用户解密。"
            f"换过机器/用户，或这是别人的文件时，请重新 hpc-otp import。")
    raise RuntimeError(f"{STORE_PATH} 不是本工具生成的文件，已拒绝读取")


def save_store(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    blob = MAGIC + _dpapi(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                          protect=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_bytes(blob)
    tmp.replace(STORE_PATH)
    if os.name == "nt":  # 收紧 ACL：只留当前用户（Windows 才有 icacls）
        try:
            os.system(f'icacls "{STORE_PATH}" /inheritance:r /grant:r "%USERNAME%:F" >nul 2>&1')
        except Exception:  # noqa: BLE001
            pass


def store_path_hint() -> str:
    return str(STORE_PATH)


# --------------------------------------------------------------------------
# TOTP
# --------------------------------------------------------------------------
def normalize_secret(secret: str) -> str:
    return secret.strip().replace(" ", "").replace("-", "").rstrip("=").upper()


def pad_secret(secret: str) -> str:
    """Base32 需要补足到 8 的倍数，否则 pyotp 会报 Incorrect padding。"""
    s = normalize_secret(secret)
    return s + "=" * (-len(s) % 8)


def make_totp(secret: str):
    import pyotp
    return pyotp.TOTP(pad_secret(secret))


def current_code(store: dict | None = None) -> str:
    store = store if store is not None else load_store()
    secret = store.get("otp_secret")
    if not secret:
        raise SystemExit("本地还没有 OTP 密钥，请先运行："
                         "hpc-otp import --qr 绑定页截图.png")
    return make_totp(secret).now()


def decode_qr(path: str | Path) -> str:
    """从二维码截图里解出 otpauth:// 或在页面显示的 Base32 密钥。"""
    import cv2  # opencv-python；你机器上已装
    img = cv2.imread(str(path))
    if img is None:
        raise SystemExit(f"读不到图片：{path}")
    det = cv2.QRCodeDetector()
    data = det.detectAndDecode(img)[0]
    if not data:  # 再试一次：放大 + 灰度，应对小截图
        big = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        data = det.detectAndDecode(cv2.cvtColor(big, cv2.COLOR_BGR2GRAY))[0]
    if not data:
        raise SystemExit(
            "二维码没解出来。建议：\n"
            "  1) 截图时把二维码连同周围留白一起截，别只截像素中心；\n"
            "  2) 在浏览器 F12 里看那张 img 的 src（常是 data:image/png;base64），"
            "把 base64 存成 png 再试；\n"
            "  3) 或者页面若直接显示了 Base32 密钥，用 --secret 传入。")
    return data


def secret_from_payload(payload: str) -> tuple[str, str]:
    """返回 (secret, label)。接受 otpauth:// URI 或裸 Base32。"""
    payload = payload.strip()
    if payload.lower().startswith("otpauth://"):
        import pyotp
        uri = pyotp.parse_uri(payload)
        return uri.secret, (getattr(uri, "name", "") or "")
    if "secret=" in payload:
        secret = payload.split("secret=")[1].split("&")[0]
        return normalize_secret(secret), ""
    return normalize_secret(payload), ""


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_selftest(_args) -> int:
    import pyotp
    try:
        from importlib.metadata import version
        ver = version("pyotp")
    except Exception:  # noqa: BLE001
        ver = "(未知版本)"
    vec = pyotp.TOTP("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", digits=8).at(59)
    ok = vec == "94287082"
    print(f"pyotp 版本: {ver}")
    print(f"RFC 6238 标准向量 @59s = {vec}（期望 94287082）-> {'OK' if ok else '失败'}")
    try:
        import cv2
        print(f"opencv 版本: {cv2.__version__}（二维码解码可用）")
    except Exception as exc:  # noqa: BLE001
        print(f"opencv 不可用（二维码截图解码会失败，可改用 --secret）：{exc}")
    print(f"凭据文件位置: {store_path_hint()}"
          f"{'（已存在）' if STORE_PATH.exists() else '（尚未创建）'}")
    return 0 if ok else 1


def mask_secret(payload: str, secret: str, show: bool) -> str:
    """默认给输出里的密钥打码，避免明文落进终端记录/日志。"""
    if show or not secret:
        return payload
    masked = f"{secret[:4]}****{secret[-4:]}" if len(secret) > 8 else "****"
    return re.sub(re.escape(secret), masked, payload, flags=re.IGNORECASE)


def mask_label(label: str) -> str:
    return f"{label[:4]}****{label[-2:]}" if len(label) > 6 else "****"


def mask_accountish(label: str) -> str:
    """标签形如 `2100012345@example` 时，把其中的账号打码。"""
    if not label:
        return label
    local, _, domain = label.partition("@")
    if local.isdigit() and len(local) >= 6:
        return mask_label(local) + (f"@{domain}" if domain else "")
    return label


def account_from_label(label: str) -> str:
    """从标签里取上机账号（学号/工号）：取 @ 前那段纯数字。"""
    local = (label or "").split("@", 1)[0].strip()
    return local if local.isdigit() and 6 <= len(local) <= 15 else ""


def cmd_import(args) -> int:
    if args.qr:
        payload = decode_qr(args.qr)
    elif args.uri:
        payload = args.uri
    elif args.secret:
        payload = args.secret
    else:
        print("需要 --qr / --uri / --secret 之一")
        return 1

    secret, label = secret_from_payload(payload)
    if args.label:
        label = args.label
    account = account_from_label(label)
    shown_uri = mask_secret(payload, secret, args.show_secret)
    if account and not args.show_secret:
        shown_uri = shown_uri.replace(account, mask_label(account))
    print(f"二维码内容: {shown_uri}")
    if len(secret) < 16:
        print(f"警告：密钥只有 {len(secret)} 位，Base32 一般 16 位以上，请确认没抄错。")

    totp = make_totp(secret)
    # 先验证能不能算出码，再落盘：否则会把一个永远用不了的密钥存进凭据文件
    try:
        current = totp.now()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 这个密钥算不出动态码（{exc}）：当前 {len(secret)} 位。\n"
              f"   Base32 密钥的有效长度必须让 (长度 % 8) ∈ {{0,2,4,5,7}}，"
              f"补位也救不了非法长度。\n"
              f"   请回去核对二维码/密钥文本是否抄漏，本地存储未被修改。")
        return 1

    store = load_store()
    store["otp_secret"] = secret
    shown_label = label or "(无)"
    if label:
        store["otp_label"] = label
        # 标签常见形态：`2100012345@example` 或纯数字，顺手记为上机账号，省掉 set-user 一步
        if account:
            store["username"] = account
            shown_label = mask_accountish(label) + "（已同时记为上机账号）"
    save_store(store)

    print(f"已保存（DPAPI 加密）-> {store_path_hint()}")
    print(f"标签: {shown_label}   密钥指纹: {secret[:4]}****{secret[-4:]}")
    print(f"当前动态码: {current}   （剩余 {30 - int(time.time()) % 30} 秒）")
    print("请拿这个码去你的集群门户 OTP 绑定页点『验证手机令牌』自检。")
    return 0


def cmd_set_user(args) -> int:
    store = load_store()
    store["username"] = args.username.strip()
    save_store(store)
    print(f"已记住上机账号: {store['username']}")
    return 0


def cmd_set_password(_args) -> int:
    pwd = getpass.getpass("门户密码（输入不显示）: ")
    again = getpass.getpass("再输入一次确认: ")
    if pwd != again:
        print("两次输入不一致，未保存。")
        return 1
    store = load_store()
    store["password"] = pwd
    save_store(store)
    print("密码已用 DPAPI 加密保存。若不想留存，可随时 clear 后改用 --ask-password。")
    return 0


def cmd_code(args) -> int:
    store = load_store()
    code = current_code(store)
    if args.quiet:
        print(code)
    else:
        print(f"{code}   （剩余 {30 - int(time.time()) % 30} 秒）")
    return 0


def cmd_verify(_args) -> int:
    store = load_store()
    if not store.get("otp_secret"):
        print("本地还没有 OTP 密钥，先跑 import。")
        return 1
    totp = make_totp(store["otp_secret"])
    print("下面会滚动显示动态码；去你的集群门户 OTP 绑定页点『验证手机令牌』，"
          "输入当前码即可。Ctrl+C 退出。")
    last = None
    try:
        while True:
            code, left = totp.now(), 30 - int(time.time()) % 30
            if code != last:
                print(f"  {code}   剩余 {left:2d}s")
                last = code
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


def cmd_show(args) -> int:
    store = load_store()
    if not store:
        print(f"{store_path_hint()} 里没有任何内容。")
        return 0
    full = getattr(args, "full", False)
    secret = store.get("otp_secret", "")
    masked = f"{secret[:4]}****{secret[-4:]}" if secret else "(无)"
    user = store.get("username") or ""
    label = store.get("otp_label") or ""
    print(f"凭据文件 : {store_path_hint()}")
    print(f"上机账号 : {user if (full or not user) else mask_label(user)}"
          f"{'' if user else '(未记录)'}")
    print(f"门户密码 : {'已保存' if store.get('password') else '(未保存，运行时询问)'}")
    print(f"OTP 密钥 : {masked}（始终打码；需要明文用 import --show-secret 重导）")
    print(f"OTP 标签 : {(label if full else mask_accountish(label)) or '(无)'}")
    if secret:
        try:
            print(f"当前动态码: {current_code(store)}")
        except Exception as exc:  # noqa: BLE001
            print(f"当前动态码: 计算失败（{exc}）—— 密钥可能抄错了，请重新 import。")
    return 0


def cmd_clear(_args) -> int:
    if STORE_PATH.exists():
        STORE_PATH.unlink()
        print(f"已删除 {STORE_PATH}")
    else:
        print("本来就没有。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="HPC 平台 OTP 密钥 / 凭据管理（密码 + TOTP）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="本机自检（pyotp / opencv / 存储位置）")

    imp = sub.add_parser("import", help="导入 OTP 密钥")
    imp.add_argument("--qr", help="绑定页二维码截图路径")
    imp.add_argument("--uri", help="otpauth:// 完整 URI")
    imp.add_argument("--secret", help="Base32 密钥")
    imp.add_argument("--label", help="备注标签（可选）")
    imp.add_argument("--show-secret", action="store_true",
                     help="输出里显示完整密钥（默认打码，仅确认抄写时才需要）")

    su = sub.add_parser("set-user", help="记住上机账号（学号/工号）")
    su.add_argument("username")
    sub.add_parser("set-password", help="记住门户密码（可选，DPAPI 加密）")

    c = sub.add_parser("code", help="打印当前动态码")
    c.add_argument("--quiet", action="store_true", help="只输出 6 位码，方便脚本调用")

    sub.add_parser("verify", help="循环显示动态码，用于网页自检")
    sh = sub.add_parser("show", help="查看已保存内容（默认打码）")
    sh.add_argument("--full", action="store_true", help="显示完整账号/标签（密钥仍然打码）")
    sub.add_parser("clear", help="删除本地凭据")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "selftest": cmd_selftest,
        "import": cmd_import,
        "set-user": cmd_set_user,
        "set-password": cmd_set_password,
        "code": cmd_code,
        "verify": cmd_verify,
        "show": cmd_show,
        "clear": cmd_clear,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
