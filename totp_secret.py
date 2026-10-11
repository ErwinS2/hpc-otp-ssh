#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容垫片：真正实现在 hpc_otp_ssh/otp.py。

保留这个文件名，是为了让已经用惯的老命令继续可用：

    python totp_secret.py code

推荐改用打包后的入口（``pip install -e .`` 之后）：

    hpc-otp code
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hpc_otp_ssh.otp import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
