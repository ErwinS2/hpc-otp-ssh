# 北大未名一号 · 实战记录

> 本文是**一个具体集群的落地记录**（北大高性能计算平台「未名一号」，SCOW 门户 + Slurm）。
> 用它演示从绑定 OTP 到提交作业的完整过程，以及踩过的坑。
> 其它集群可对照参考，命令里的 `<...>` 换成你的实际值即可。

配套命令见 [README](../README.md)，给 agent 看的调用规范见 [agent-usage.md](agent-usage.md)。

配套文件：`hpc-otp`（管密钥）、`hpc-ssh`（管连接）。所有命令都在 **PowerShell** 里执行。

---

## 0. 前提自检（先花 2 分钟确认，别急着往下做）

| # | 要确认的事 | 怎么确认 | 不满足怎么办 |
|---|---|---|---|
| 1 | 你**已经有上机账号**（学号/工号）和**门户密码** | 打开 <https://hpc.pku.edu.cn/authotp/otp> 能登录进去 | 还没开户：下载[用户申请表](https://hpc.pku.edu.cn/docs/20170829214610609696.docx)盖章，交理科一号楼 1 层 1150S，约 2 个工作日 |
| 2 | 网络能到集群 | `Test-NetConnection wm1-login.pku.edu.cn -Port 22` 的 `TcpTestSucceeded` 是 `True` | 校外先连北大 VPN：<https://its.pku.edu.cn/service_1_proxy.jsp> |
| 3 | 本机依赖齐了 | `cd <repo>; hpc-otp selftest` | 应看到 pyotp / opencv 都正常 |

> 若你的账号是**公共账号**开通的上机账号：OTP 绑定不能自助做，需以《公共上机账号OTP绑定》为邮件名发账号信息到 hpc@pku.edu.cn；且**不能使用 SCOW 可视化平台**，只能走 SHELL。

---

## 1. 把手机里的 OTP 密钥搬进电脑（唯一有点绕的一步）

### 为什么必须做

TOTP 是标准算法（HMAC-SHA1 / 30 秒 / 6 位），**密钥**才是第二因素，手机 App 只是它的容器。
只要电脑上有同一把密钥，两边算出的码永远一致、互不干扰。这一步做完，电脑才能"替你在场"。

### 1.1 打开绑定页并登录

浏览器访问 <https://hpc.pku.edu.cn/authotp/otp>，用**学校门户账号（IAAA）**登录。
⚠️ 注意：IAAA 令牌 ≠ HPC 的 OTP，别混。文档还特别提醒"请务必使用和集群账号相同的学号绑定 OTP"。

### 1.2 点「重新绑定手机令牌」

页面会显示一个二维码。

⚠️ **这一步会生成新密钥**：你手机上原来那个绑定**立即失效**，所以第 1.3 步必须马上重扫。

### 1.3 手机 FreeOTP 重新扫这个二维码

- 打开 FreeOTP → 扫描 → 扫屏幕上这个新二维码。
- 若提示 `Your token provider has not configured the issuer field...`，随便起个名字当备注即可。
- 扫完手机上就有新令牌了，手机这条路恢复正常。

### 1.4 同时把密钥在电脑上留一份

**情况 A：页面直接显示了 Base32 密钥文本**（一串大写字母+数字，可能带空格）
→ 直接抄下来，跳到 1.5 用 `--secret`。

**情况 B：只有二维码**（更常见）→ 截图后让脚本解：

```powershell
cd <repo>
hpc-otp import --qr 绑定页.png
```

截图要注意的三点：
1. **把二维码和周围留白一起截**，别只截二维码中心那几个像素；
2. 别缩放、别压缩、别加滤镜，尽量原图；
3. 文件名别带空格（或加引号）。

如果脚本说"二维码没解出来"，还有两条备用路：
- **F12 大法**：在绑定页按 `F12` → `Elements` → 找到那个 `<img>`，看 `src`。常见是 `data:image/png;base64,....`，把 `base64,` 后面那段存成 png 再喂给 `--qr`；有些实现会在页面 JS 里直接放着 `otpauth://totp/...`，看到就能直接用 `--uri`。
- **直接抄**：部分页面在二维码下方会写密钥（有时要点"无法扫描？"之类）。

导入成功应该看到（这是我实测的输出形态）：

```
二维码内容: otpauth://totp/PKUHPC:2100012345?secret=JBSWY3DPEHPK3PXP&issuer=PKUHPC
已保存（DPAPI 加密）-> <repo>\cred.dat
标签: (无)   密钥指纹: JBSW****3PXP
当前动态码: 516424   （剩余 18 秒）
请拿这个码去 https://hpc.pku.edu.cn/authotp/otp 点『验证手机令牌』自检。
```

真实平台上的标签形如 `2100012345@pkuhpc`，脚本会自动把 `@` 前的学号记成上机账号
（输出里打码显示），所以顺利的话**第 2 步可以跳过**，用 `hpc-otp show` 确认即可。

> 🔒 **导入成功后建议删掉二维码截图**。密钥已经加密进 `cred.dat`，而那张 png 是**明文密钥**，
> 是本机最薄弱的一环：`Remove-Item <repo>\qr.png`。
> 删了不影响使用；真要重来，去网页再绑定一次就有新二维码了。

### 1.5 自检（**这一步千万别跳过**）

立刻拿刚打印的那个 6 位码，回到绑定页点「**验证手机令牌**」并输入：

- ✅ **验证通过** → 电脑上的密钥正确，可以往下走了。
- ❌ **验证失败** → 依次排查：
  1. 是不是**过了 30 秒**（码过期了，重新跑 `hpc-otp verify` 会滚动显示当前码）；
  2. 电脑时钟准不准（TOTP 对时间敏感，Windows 时间同步要开着）；
  3. 是不是抄错了字符（重新 `import`）；
  4. 扫的是不是**新的**那个二维码。

也建议顺手在手机上确认同一个码能对上（两边一致=绑定成功）。

### 1.6 万一密钥丢了/想重来

重新绑定一次手机令牌即可（会生成又一把新密钥），然后重新 `import`。
密钥本身随时可通过 `hpc-otp clear` 从本机删除。

---

## 2. 记住账号（和可选密码）

> 如果第 1.4 步的二维码标签里带了学号（形如 `2100012345@pkuhpc`），账号**已经自动记好**，
> 这一步只需决定密码怎么处理（下面的表格）。

```powershell
cd <repo>
hpc-otp set-user 2100012345     # 如需手工指定/覆盖，换成你的学号/工号
```

输出：`已记住上机账号: 2100012345`

密码有两种模式，挑一个：

| 模式 | 命令 | 特点 |
|---|---|---|
| **每次询问**（推荐先用这个） | 不用做任何事 | 密码不落盘，但每次运行 `hpc-ssh` 都要手输一次 |
| 记住密码 | `hpc-otp set-password` | 无人值守方便；密码用 DPAPI 加密存本机 |

看看现在存了什么（掩码显示，不会打印明文）：

```powershell
hpc-otp show
```

---

## 3. 首次登录

```powershell
hpc-ssh check --yes
```

会发生什么：
1. 首次连接新主机，脚本打印主机密钥指纹（`SHA256:...`）并记入 `~/.ssh/wm1_known_hosts`；`--yes` 表示自动接受。**不加 `--yes`** 时会在交互终端里问你 `y/N`。
2. 认证：密码 + OTP 由脚本自动作答（服务器的两次提问会被自动识别）。
3. 成功后打印远端信息，形态大致如下（真实内容以集群为准）：

```
2100012345
login01
/home/2100012345
---
PARTITION   AVAIL  TIMELIMIT  NODES  STATE  NODELIST
C032M0128G     up     5-00:00:00  140   idle  a4u03n[01-140]
...
[已登录 2100012345@wm1-login.pku.edu.cn，退出码 0]
```

**看到 whoami 输出 = 全链路打通。**

如果失败，对照这里：

| 提示 | 含义 | 处理 |
|---|---|---|
| `连不上 ...:22 ...` | 网络不通 | 连校园网/VPN 后重试 |
| `认证失败（...）。常见原因...` | 密码或动态码不对 | 先手工 `ssh 学号@wm1-login.pku.edu.cn` 登一次，确认密码/OTP 本身没问题 |
| `缺少 paramiko` | 依赖没装 | `python -m pip install paramiko` |
| `没有上机账号` / `没有 OTP 密钥` | 第 1、2 步没做 | 回到第 1、2 步 |
| `⚠ ... 的主机密钥与上次不同！` | 平台换过密钥（或中间人） | 确认后删 `C:\Users\31507\.ssh\wm1_known_hosts` 重连 |
| 动态码一直不对，但手机能登 | 提问文案没被识别 | 加 `--debug-auth` 看服务器原文，再调整 |

> `hpc-ssh` **认证失败不重试**（故意这样设计，避免触发账号封锁）。失败就人工看一下，别写循环重试。

---

## 4. 日常操作

### 4.1 先摸清自己有什么资源

```powershell
hpc-ssh run 'sacctmgr show ass user=$(whoami) format=account%15 | uniq'
hpc-ssh run 'sacctmgr show ass user=$(whoami) format=partition%15,qos%15'
hpc-ssh run 'sinfo -s'
```

> **PowerShell 引号坑（实测过，务必看）**。远端命令要经过两层解析，写错会报
> `unrecognized arguments`：
>
> | 写法 | hpc-ssh 实际收到的参数 | 结果 |
> |---|---|---|
> | `run 'echo "a b"'` | `['echo a', 'b']` | ❌ 被空格拆开 |
> | `run 'echo ''a b'''` | `["echo 'a b'"]` | ✅ 里层改用**两个单引号** |
> | `run 'echo a\ b'` | `['echo a\ b']` | ✅ 反斜杠转义空格 |
> | `run 'sinfo -o %P,%a,%l'` | `['sinfo -o %P,%a,%l']` | ✅ 干脆不用空格/引号 |
>
> `$USER`、`$(whoami)` 这类远端变量必须用**单引号**包命令，否则本地 PowerShell 先展开。
> 实测：`"sacctmgr ... $(whoami)"` 会把你的**本地 Windows 用户名**塞进去，变成
> `sacctmgr ... user=your-pc\you`。要写死就用 `-u 2100012345`。

### 4.2 提交作业（这才是正经用法）

在 `<repo>` 下建一个 `job.sh`：

```bash
#!/bin/bash
#SBATCH -o job.%j.out
#SBATCH -J myjob
#SBATCH --partition=C032M0128G
#SBATCH --account=你的账户名
#SBATCH --qos=low
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=8
#SBATCH --time=02:00:00

module purge
hostname
echo "hello from $(hostname)"
```

- `--account` 用 4.1 第一条命令查到的值填；不填会报 `Invalid account or account/partition combination`。
- 选分区请对着 4.1 的输出和[分区表](https://hpc.pku.edu.cn/ug/cluster/wm1/)（GPU 分区按"1 块卡配 6 核"口径申请更省钱）。
- 未名一号单作业最长 5 天（QOS 限制）。

提交、跟踪、取日志：

```powershell
hpc-ssh run 'mkdir -p ~/jobs'                    # 首次先建好远端目录
hpc-ssh submit .\job.sh --remote-dir ~/jobs
hpc-ssh run 'squeue -u 2100012345'
hpc-ssh run 'sacct -u 2100012345 --starttime today --format=JobID,JobName,State,Elapsed'
hpc-ssh run 'tail -30 ~/jobs/job.1234.out'
```

`submit` 成功会打印 `[已提交作业 1234]` 并给出跟踪命令。

### 4.3 传文件

大数据/需要联网的场景走数据传输节点：

```powershell
hpc-ssh put .\dataset.tar ~/       --host data
hpc-ssh get ~/result.tar .\        --host data
```

> 🔑 **首次用 `--host data` 要加 `--yes`**：`wm1-data` 是和 `wm1-login` **不同的主机**，
> 有自己的主机密钥要确认。第一次会打印它的指纹并要求确认，非交互环境必须显式放行：
>
> ```powershell
> hpc-ssh put .\data.tar ~/ --host data --yes     # 只需第一次，之后已记住
> ```
>
> 实测 `~/` 已能正确展开成 `/gpfs/share/home/<学号>`，登录节点与数据节点**共享同一份存储**，
> 在数据节点上传的文件在登录节点立刻可见。

小文件直接走登录节点也行（`--host login`，默认）。

### 4.4 交互式调试（要进计算节点）

方式一，用脚本的 shell：

```powershell
hpc-ssh shell
```

登进去后就是登录节点，按平台的交互式流程走：

```bash
salloc -p C032M0128G -N1 -n6 -q low -t 2:00:00     # 申请到节点后，例如返回 a4u03n07
ssh a4u03n07                                        # 进计算节点调试
exit                                                 # 调完退出计算节点
scancel 1078858                                     # 重要：用完立刻释放，避免继续计费
squeue -j 1078858                                   # 确认作业已退出
```

方式二（**日常更推荐**）：直接用 Windows 自带 ssh，终端体验更好（颜色/窗口自适应/Ctrl 组合键都正常）：

```powershell
ssh 2100012345@wm1-login.pku.edu.cn
```

`hpc-ssh` 的价值在**自动化**；纯人肉交互用原生 ssh 更舒服。

### 4.5 这些事不要做

- ❌ 在**登录节点**跑计算（跑得慢还可能违规），要算力就 `sbatch` / `salloc`。
- ❌ 让脚本循环重试认证。
- ❌ `conda install` / `pip install` 往**共享环境**里装东西，装到自己的环境/目录。
- ❌ 忘记 `scancel` 交互式作业 —— 那是按核时**计费**的。

---

## 5. 把它接到 agent 上

### 5.1 调用契约

对 agent 只暴露 `run --json`，输出可直接解析：

```powershell
hpc-ssh run --json 'squeue -u $USER'
```

```json
{"host":"wm1-login.pku.edu.cn","user":"2100012345","command":"squeue -u $USER",
 "exit_code":0,"stdout":"JOBID PARTITION ...","stderr":""}
```

进程退出码约定（agent 要据此判断，而不是猜）：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 远端命令的退出码 | 命令本身失败 |
| 3 | 缺 paramiko |
| 4 | 主机密钥未知/变化 |
| 5 | 网络不通（校园网/VPN） |
| 6 | **认证失败 → 立即停止，不要重试** |
| 7 | 非交互环境没有可用密码 → 先 `hpc-otp set-password` |
| 124 | 超时（本地等待超时，通道已断） |

### 5.2 贴给 agent 的工具说明（可直接复用）

```text
你可以通过本机 PowerShell 操作北大未名一号 HPC 集群，工具如下：

读取/执行：python <repo>\hpc-ssh run --json "<远端命令>"
提交作业：  python <repo>\hpc-ssh submit <本地脚本> --remote-dir ~/jobs
传文件：    python <repo>\hpc-ssh put|get <...> --host data

规则：
1. 远端命令里若要用 $USER、$(hostname) 等变量，整条命令必须放在单引号里，
   避免被本地 PowerShell 提前展开。
2. 退出码 6 = 认证失败：立刻停止并报告，绝不重试（会锁账号）。
   退出码 5 = 网络问题，提示用户连 VPN/校园网。
3. 登录节点不可联网且不用于计算：要算力就写 sbatch 脚本提交，并用
   squeue/sacct 跟踪，最后用 tail 取输出。
4. 不要执行破坏性命令（rm -rf、chmod -R、批量删除、改共享环境）。
5. 交互式作业（salloc）用完必须 scancel，否则持续计费。
6. 每次动作后核对结果，长命令用 --timeout 限制等待时间。
```

### 5.3 典型闭环（agent 视角）

1. `run --json 'squeue -u $USER'` 看队列闲忙；
2. 生成 `job.sh`（带上用户的 account/partition/qos）→ `submit`；
3. `run --json 'sacct -u $USER --starttime today'` 轮询状态；
4. 作业完成后 `run 'tail -50 ~/jobs/job.<id>.out'` 取结果；
5. 需要结果文件就 `get --host data`。

### 5.4 权限边界（想清楚再放行）

- `cred.dat` + 脚本 + 一台能连校园网的机器 = **你账号的全部上机权限**。别把 `cred.dat` 外发、上传、进 git。
- 建议给 agent 限定工作目录（如 `~/jobs`、`~/scratch`），并在提示里写死"不得删除数据"。
- 想让 agent 更安全：让**它只写脚本、人来按提交**（`submit` 换成 `put`），凭据风险瞬间归零。

---

## 6. 撤销与清理

| 目标 | 做法 |
|---|---|
| 让本机忘记密钥/密码 | `hpc-otp clear` |
| 作废电脑上那份密钥 | 网页重新绑定手机令牌（生成新密钥，旧的立刻失效） |
| 卸载依赖 | `python -m pip uninstall paramiko` |
| 彻底删除 | 删除整个 `<repo>` 文件夹 + 删 `C:\Users\31507\.ssh\wm1_known_hosts` |
| 销户 | 联系平台管理员（见[开户页](https://hpc.pku.edu.cn/ug/account/account/)） |

---

## 7. 平台硬限制速记

- **登录节点不可联网**（只用于提交任务）；数据传输节点 `wm1-data` 可联网、能访问存储、**不能提交作业**。
- **不支持公钥登录** → 一切自动化都必须带密码 + OTP。
- 未名一号单作业最长 **5 天**，QOS 分 `low/normal/high`（优先级 100/500/1000）。
- 校外必须先连校园网 / 北大 VPN。
- 公共账号开通的上机账号：只能 SHELL，不能上 SCOW；OTP 绑定要找管理员。
- 平台**没有**对用户开放的作业/节点 API —— 本手册这套"密码+OTP 的 SSH 自动化"是当前唯一可行路径，
  是否被允许请自行向 hpc@pku.edu.cn 确认。
