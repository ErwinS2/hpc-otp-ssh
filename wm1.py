#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容垫片：真正实现在 hpc_otp_ssh/connector.py。

保留这个文件名，是为了让已经用惯的老命令继续可用：

    python wm1.py run --json 'squeue -u $USER'

推荐改用打包后的入口（``pip install -e .`` 之后）：

    hpc-ssh run --json 'squeue -u $USER'
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hpc_otp_ssh.connector import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
