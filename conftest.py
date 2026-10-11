# -*- coding: utf-8 -*-
"""pytest 全局配置。

注意：所有涉及凭据的测试都必须用 ``temp_store`` 夹具，把存储路径指到临时目录，
绝不读写开发者本机真实的 ``cred.dat``。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hpc_otp_ssh import otp as otp_mod  # noqa: E402


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    """把凭据文件重定向到临时目录，返回该路径。"""
    path = tmp_path / "cred.dat"
    monkeypatch.setattr(otp_mod, "STORE_PATH", path)
    return path
