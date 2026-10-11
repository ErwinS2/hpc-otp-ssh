# -*- coding: utf-8 -*-
"""hpc-otp-ssh：面向 HPC 集群的「密码 + TOTP」SSH 自动化，带 agent 友好的 JSON CLI。

两个模块：

* :mod:`hpc_otp_ssh.otp`       —— 密钥/凭据管理（二维码导入、DPAPI 加密存储、生成动态码），CLI 名 ``hpc-otp``
* :mod:`hpc_otp_ssh.connector` —— SSH 连接器（认证作答、执行命令、提交作业、传文件），CLI 名 ``hpc-ssh``
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
