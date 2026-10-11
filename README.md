# hpc-otp-ssh

Automate password + TOTP (MFA) SSH logins to HPC clusters that expose no API, and drive them from
scripts or LLM agents. Windows only — credential storage uses DPAPI.

面向 HPC 集群的「密码 + OTP 动态码」SSH 自动化工具，输出对脚本与 LLM agent 友好。

适用场景：集群不提供对外 API、不支持公钥登录，只允许密码 + OTP 交互式认证，
但你需要自动提交作业、查队列、取日志，或把集群交给 agent 操作。

原理：TOTP 是标准算法，手机 App 只是密钥的容器。把绑定二维码里的同一密钥取到本机
（DPAPI 加密保存），登录时自动回答服务器的两次提问（密码 + 动态码）。

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows（凭据加密用 DPAPI；Linux/macOS 后端未实现） |
| Python | ≥ 3.9 |
| 账号 | 已开通上机账号，并已在门户绑定 TOTP 动态令牌 |
| 网络 | 能访问集群（校内网，或机构 VPN） |
| 依赖 | `paramiko`、`pyotp`（安装时自动装）；从二维码截图导入需额外装 `opencv-python` |

## 安装

```bash
git clone https://github.com/ErwinS2/hpc-otp-ssh.git
cd hpc-otp-ssh

pip install -e .            # 源码目录内安装（推荐，便于改代码）
pip install -e ".[qr]"      # 需要「从二维码截图导入密钥」时
```

尚未发布到 PyPI，因此**不能**用 `pip install hpc-otp-ssh` 安装，请用上面的源码方式。

安装后获得两个命令：`hpc-otp`（管理密钥与凭据）、`hpc-ssh`（连接集群）。

若提示命令不存在，说明 Python 的 `Scripts` 目录不在 PATH 上，可改用模块方式调用：

```bash
python -m hpc_otp_ssh.otp selftest
python -m hpc_otp_ssh.connector run --json 'squeue -u $USER'
```

## 使用步骤

### 1. 把 OTP 密钥导入本机

在门户的 OTP 绑定页点「重新绑定手机令牌」，页面会显示二维码。用手机 App 扫它
（保持手机可用），同时把二维码截图保存为 png，然后：

```bash
hpc-otp import --qr binding-page.png
hpc-otp import --secret JBSWY3DPEHPK3PXP   # 或页面直接给出 Base32 密钥时
```

命令会打印当前动态码。拿它到门户点「验证手机令牌」，通过即说明本机密钥正确。

导入成功后请删除那张截图——它相当于明文密钥。

### 2. 记住账号与密码

```bash
hpc-otp set-user 2100012345    # 学号/工号；二维码标签形如 2100012345@issuer 时会自动记住
hpc-otp set-password           # 密码用 DPAPI 加密保存在本机；不设则每次运行都要输入
hpc-otp show                   # 查看已保存内容（密钥与账号默认打码）
```

### 3. 首次连接

```bash
hpc-ssh check --yes
```

首次连接会打印主机密钥的 SHA256 指纹并记入 `~/.ssh/hpc_otp_ssh_known_hosts`。
`--yes` 表示接受；不加则会在交互终端里询问。随后打印远端的 `whoami`/`hostname`/`sinfo`。

### 4. 日常命令

```bash
hpc-ssh run --json 'squeue -u $USER'                    # 执行命令并返回 JSON
hpc-ssh run 'sacct -u $USER --starttime today'          # 直接看输出
hpc-ssh submit ./examples/job.sh --remote-dir ~/jobs    # 上传脚本并 sbatch
hpc-ssh run 'tail -50 ~/jobs/job.<作业号>.out'          # 查看作业输出
hpc-ssh put ./data.tar ~/ --host data                   # 经数据传输节点上传
hpc-ssh get '~/result.tar' ./ --host data               # 下载
hpc-ssh shell                                           # 交互式 shell
```

首次使用 `--host data` 需要加一次 `--yes`：数据传输节点是与登录节点不同的主机，有独立密钥。

作业脚本模板见 [examples/job.sh](examples/job.sh)。提交前先用这两条命令确认账户、分区与 QOS：

```bash
hpc-ssh run 'sacctmgr show ass user=$(whoami) format=account%15,partition%15,qos%15'
hpc-ssh run 'sinfo -o %P,%a,%l,%D,%t'
```

## 命令参考

### hpc-otp

| 命令 | 说明 |
|---|---|
| `import --qr FILE` | 从二维码截图导入密钥（需 opencv） |
| `import --secret STR` / `--uri URI` | 直接给 Base32 密钥或 otpauth URI |
| `set-user NAME` | 记住上机账号 |
| `set-password` | 记住门户密码（DPAPI 加密） |
| `code [--quiet]` | 输出当前动态码 |
| `verify` | 滚动显示动态码，用于门户核对 |
| `show [--full]` | 查看已保存内容（密钥始终打码） |
| `clear` | 删除本机凭据 |
| `selftest` | 检查依赖与存储位置 |

### hpc-ssh

| 命令 | 说明 |
|---|---|
| `check` | 健康检查：认证并打印 `whoami`/`hostname`/`sinfo` |
| `run [--json] [--timeout N] CMD` | 在登录节点执行命令 |
| `submit SCRIPT [--remote-dir ~/jobs]` | 上传脚本并 `sbatch`，自动把 CRLF 转成 LF |
| `put LOCAL REMOTE` / `get REMOTE LOCAL` | 传文件，建议配 `--host data` |
| `shell` | 交互式 shell（会阻塞等人输入，脚本与 agent 不要用） |

全局参数（放在子命令前后都可以）：`--user`、`--host login|data`、`--ask-password`、
`--save-password`、`--yes`、`--debug-auth`。

## 输出格式与退出码

`run --json` 的输出：

```json
{"host": "wm1-login.pku.edu.cn", "user": "2100012345", "command": "squeue -u $USER",
 "exit_code": 0, "timed_out": false, "stdout": "...", "stderr": "..."}
```

进程退出码：

| 码 | 含义 | 建议处理 |
|---|---|---|
| 0 | 成功 | — |
| 其他 | 远端命令的退出码 | 读 `stderr` |
| 3 | 缺少 `paramiko` | 安装依赖 |
| 4 | 主机密钥未知或已变化 | 未知→确认后 `--yes`；变化→先核对指纹 |
| 5 | 网络不通 | 检查校内网 / VPN |
| 6 | 认证失败 | 不要重试（可能锁账号），先人工核对密码与动态码 |
| 7 | 没有可用密码且无法交互输入 | 先执行 `hpc-otp set-password` |
| 124 | 本地等待超时 | 加大 `--timeout`，或改为提交作业后轮询状态 |

## 配置

| 环境变量 | 说明 |
|---|---|
| `HPC_LOGIN_HOST` | 登录节点域名（默认 `wm1-login.pku.edu.cn`） |
| `HPC_DATA_HOST` | 数据传输节点域名（默认 `wm1-data.pku.edu.cn`） |
| `HPC_PORT` | SSH 端口，默认 22 |
| `HPC_OTP_STORE` | 凭据文件路径（默认仓库根目录的 `cred.dat`） |
| `HPC_KNOWN_HOSTS` | 主机密钥记录文件（默认 `~/.ssh/hpc_otp_ssh_known_hosts`） |

只要平台使用「密码 + TOTP 的 keyboard-interactive 认证」，改这几个变量即可用于其它集群。

## 给 agent 使用

调用规范、约束清单、可直接粘贴的系统提示词与 function-calling schema 见
[docs/agent-usage.md](docs/agent-usage.md)。

最小用法：

```bash
hpc-ssh run --json 'squeue -u $USER'
```

## 排错

| 现象 | 处理 |
|---|---|
| 认证失败（退出码 6） | 先用系统 `ssh` 手工登录一次，确认密码与动态码本身没问题；不要循环重试 |
| 连不上（退出码 5） | 确认在校园网内或已连 VPN |
| 主机密钥与上次不同 | 平台换过密钥则删除 known_hosts 后重连；否则警惕中间人 |
| 动态码总是错 | 检查本机时钟是否准确（TOTP 依赖时间） |
| 导入报 `Incorrect padding` | 密钥抄错或长度非法，重新导入 |
| `unrecognized arguments` | PowerShell 引号问题，见下表 |
| `Invalid account or account/partition` | 用 `sacctmgr show ass` 查正确的 account/partition |
| `QOSNotAllowed` 或超过时长限制 | 用 `sacctmgr show qos` 查可用 QOS 与最大时长 |
| 找不到 `hpc-ssh` 命令 | `Scripts` 目录不在 PATH，改用 `python -m hpc_otp_ssh.connector` |
| 作业脚本报 `/bin/bash^M` | 提交时已自动转 LF；手工上传需自行转换 |

PowerShell 下传递远端命令的引号规则：

| 写法 | 结果 |
|---|---|
| `run 'squeue -u $USER'` | 正确：由远端展开 `$USER` |
| `run "squeue -u $USER"` | 错误：本地 PowerShell 先展开，变量变成空串 |
| `run 'echo ''a b'''` | 正确：内层引号写成两个单引号 |
| `run 'sinfo -o %P,%a,%l'` | 正确：不含空格与引号，最省事 |

## 安全说明

- `cred.dat` 保存密码与 OTP 密钥，等价于该账号的完整上机权限。不要放进 git、同步盘或备份。
- 把 OTP 密钥存到本机，等于把第二因素降级为「本机文件」。是否允许脚本化登录，各集群规定不同，
  请先向管理方确认。
- 认证失败只尝试一次，不重试，避免累计失败次数导致账号被锁。
- 首次连接必须核对主机指纹；指纹变化时工具会拒绝连接。
- 二维码截图是明文密钥，导入成功后请删除（回收站一并清空）。
- 最保守的用法：agent 只负责生成脚本（`put`），由人执行 `submit`。

## 已知限制

- 凭据后端目前只有 Windows DPAPI，Linux/macOS 上会直接报错。
- 二维码导入依赖 `opencv-python`（可选依赖 `qr`）；没有它时可用 `--secret` 手工输入密钥。
- 平台若使用短信、推送或硬件令牌等非 TOTP 二次验证，本工具无法自动化。
- `shell` 子命令的终端体验不如系统自带 `ssh`，它的用途是让程序内开 PTY。

## 项目结构

```
hpc_otp_ssh/otp.py         密钥与凭据管理（hpc-otp）
hpc_otp_ssh/connector.py   SSH 连接器（hpc-ssh）
tests/                     68 项单元测试，全部离线，不连接集群
docs/agent-usage.md        agent 调用规范
docs/pku-walkthrough.md    北大未名一号实战记录
examples/job.sh            Slurm 作业模板
wm1.py, totp_secret.py     兼容垫片，转发到包内实现
```

## 开发

```bash
pip install -e ".[dev,qr]"
python -m pytest            # 68 项，全部离线
```

## License

[MIT](LICENSE)
