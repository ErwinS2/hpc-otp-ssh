# Agent 工具说明（hpc-ssh / hpc-otp）

面向 **LLM agent / 脚本**的调用规范。给人看的上手步骤见 [北大实战记录](pku-walkthrough.md)，
安装与背景见 [README](../README.md)。

> 命令名：本文用打包后的 console script（`hpc-ssh` / `hpc-otp`）。
> 如果你直接跑源码，对应关系是 `hpc-ssh` → `python wm1.py`，`hpc-otp` → `python totp_secret.py`。

---

## 0. 给 agent 的三句话

1. **只有一个入口需要记**：`hpc-ssh run --json "<远端命令>"` —— 在集群登录节点上执行命令，返回结构化结果。
2. **退出码就是语义**，不要靠猜 stdout（见 §3.3）。看到 `6` 必须停下并报告，**绝不允许重试**。
3. 登录节点**不是计算节点**：任何真正的算力需求都要 `submit` 一个 sbatch 作业，再用 `squeue`/`sacct` 跟踪。

---

## 1. 前置条件（agent 启动前应确认）

| 检查 | 命令 | 期望 |
|---|---|---|
| 凭据可用 | `hpc-otp show` | 有上机账号、OTP 密钥；密码"已保存"或允许交互输入 |
| 依赖就绪 | `hpc-ssh check --yes` | 返回远端 `whoami`/`hostname`/`sinfo`，退出码 0 |

若 `hpc-otp show` 显示缺密钥/账号 → 需要人类先做一次性绑定（见[北大实战记录](pku-walkthrough.md)第 1 步），agent 无法代劳。
若 `hpc-ssh check` 返回 `7` → 密码未保存；请人类执行 `hpc-otp set-password`（交互输入，密码不落对话）。

---

## 2. 接入方式

### 2.1 CLI（最通用）

```bash
hpc-ssh run --json 'squeue -u $USER'
```

### 2.2 Function calling / tool use

把 §9 的 JSON schema 直接注册为工具（`hpc_run` / `hpc_submit` / `hpc_put` / `hpc_get`）。
每个工具的返回值就是 `run --json` 的那个对象。

### 2.3 任何语言的子进程

```python
import json, subprocess
p = subprocess.run(["hpc-ssh", "run", "--json", "squeue -u $USER"],
                   capture_output=True, text=True)
result = json.loads(p.stdout)          # 注意：退出码看 p.returncode，不是 result["exit_code"]
```

---

## 3. 工具契约

### 3.1 `hpc-ssh run --json <命令>`

在**登录节点**上执行一条 shell 命令。

```json
{
  "host": "wm1-login.pku.edu.cn",
  "user": "2100012345",
  "command": "squeue -u $USER",
  "exit_code": 0,
  "timed_out": false,
  "stdout": "JOBID PARTITION NAME ...\n",
  "stderr": ""
}
```

| 字段 | 说明 |
|---|---|
| `exit_code` | 远端命令的退出码；**超时时为 `null`** |
| `timed_out` | `true` 表示本地等待超时、通道已断，命令可能仍在远端运行 |
| `stdout` / `stderr` | 已按 UTF-8 尽力解码 |

参数：`--timeout <秒>`（默认 120）、`--host login|data`、`--user <账号>`、`--yes`。

### 3.2 其他子命令

| 命令 | 用途 | 备注 |
|---|---|---|
| `hpc-ssh check --yes` | 探活：认证 + `whoami`/`hostname`/`sinfo` | 会话开始时的健康检查 |
| `hpc-ssh submit <本地脚本> --remote-dir ~/jobs` | 上传并 `sbatch` | 成功时 stdout 含 `Submitted batch job <id>` |
| `hpc-ssh put <本地> <远端> --host data` | 上传文件 | 首次需 `--yes`（数据节点是另一台主机） |
| `hpc-ssh get <远端> <本地> --host data` | 下载文件 | 同上 |
| `hpc-ssh shell` | 交互式 shell | **agent 不要用**（会阻塞等待人工输入） |
| `hpc-otp code --quiet` | 只输出 6 位动态码 | 一般不需要 agent 调用，`hpc-ssh` 内部已自动处理 |

### 3.3 退出码语义（agent 必须按此决策）

| 码 | 含义 | agent 应当做什么 |
|---|---|---|
| `0` | 成功 | 继续 |
| 其他数字 | **远端命令**的退出码 | 读 `stderr` 解释原因；多数情况是命令本身错 |
| `3` | 缺 `paramiko` | 报告环境问题，停止 |
| `4` | 主机密钥未知或**与上次不同** | 未知→请求人类确认后加 `--yes`；**变化→立即停止并告警**（可能是中间人） |
| `5` | 网络不通 | 提示需要校园网/VPN，停止 |
| `6` | **认证失败** | **立即停止，绝不重试**（重试会累计失败次数、可能锁账号），把原文报告给人 |
| `7` | 无法交互输入密码 | 请人类执行 `hpc-otp set-password` |
| `124` | 本地超时 | 用更长的 `--timeout` 或改成异步轮询（提交作业 + 定期查状态） |

### 3.4 命令字符串的引号规则（**最容易出错的地方**）

远端命令会经过两层解析（本地 shell → 远端 bash）：

| 场景 | 写法 |
|---|---|
| 需要远端变量 `$USER` / `$(hostname)` | **整条命令用单引号**：`run --json 'squeue -u $USER'` |
| 命令内部还需要引号 | 外层单引号 + **里层用两个单引号**：`run 'echo ''a b'''` |
| 避免引号 | 用逗号代替空格：`run 'sinfo -o %P,%a,%l'` |

⚠️ 禁止写成 `run --json "squeue -u $USER"` —— 在 PowerShell 里 `$USER` 会被**本地**展开成空串；
`"$(whoami)"` 更危险，会变成你的本地用户名。

---

## 4. 推荐工作流

```
① 健康检查      hpc-ssh check --yes
② 看资源        hpc-ssh run --json 'sinfo -o %P,%a,%l,%D,%t'
                hpc-ssh run --json 'sacctmgr show ass user=$(whoami) format=account%15,partition%15,qos%15'
③ 写作业脚本    本地生成 job.sh（必须含 --account / --partition / --qos）
④ 提交          hpc-ssh run 'mkdir -p ~/jobs'
                hpc-ssh submit ./job.sh --remote-dir ~/jobs
⑤ 轮询          hpc-ssh run --json 'squeue -j <id>'
                hpc-ssh run --json 'sacct -j <id> --format=JobID,State,Elapsed,ExitCode'
⑥ 取结果        hpc-ssh run 'cat ~/jobs/job.<id>.out'
                hpc-ssh get '~/jobs/result.tar' ./ --host data
⑦ 收尾          确认作业已退出（避免交互式作业持续计费）
```

作业脚本模板（`#SBATCH` 指令必须在文件头部）：

```bash
#!/bin/bash
#SBATCH -o job.%j.out
#SBATCH -J myjob
#SBATCH --partition=<分区>
#SBATCH --account=<账户>
#SBATCH --qos=low
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=8
#SBATCH --time=02:00:00
hostname
```

---

## 5. 红线（硬约束，写进系统提示）

1. **不许**在登录节点上跑计算（登录节点是共享的提交入口，且通常不可联网）。要算力就 `submit`。
2. **不许**对认证失败（退出码 6）做任何重试或换密码尝试。
3. **不许**执行破坏性命令：`rm -rf`、`chmod -R`、批量删除、改动共享环境（`module`/conda 公共目录）。
4. **不许**把 `cred.dat`、密码、OTP 密钥读出来、打印、上传或提交进 git。
5. **不许**用 `hpc-ssh shell`（阻塞式交互，会挂住 agent）。
6. 交互式资源（`salloc` 申请到的节点）**用完必须 `scancel`**，否则持续计费。
7. 不确定的命令先 `run --json` 只读试一次，不要凭猜测批量执行。

---

## 6. 反模式

| 反模式 | 后果 | 正确做法 |
|---|---|---|
| 在登录节点 `python train.py` | 拖慢共享登录节点，可能违规 | 写 `job.sh` + `submit` |
| `run` 里塞 `sleep 3600` | 本地超时、通道断、无法判断远端状态 | 提交作业 + 轮询 `sacct` |
| 用 `nohup ... &` 在登录节点跑长任务 | 进程被清理、结果丢失 | `sbatch` |
| 解析 `stdout` 猜成功与否 | 误判 | 看 `exit_code` |
| 认证失败后循环重试 | 可能锁账号 | 立即停止、报告人类 |
| 把 `~/` 写成 `$HOME/` 却不加引号 | 本地展开成 Windows 路径 | 用 `~/` 或写绝对路径 |

---

## 7. 排错决策树

```
run 失败
├─ exit 6  → 停止。报告："认证失败，需要人工核对密码/动态码/账号状态"
├─ exit 7  → 停止。请人类执行 set-password
├─ exit 5  → 停止。提示连校园网/VPN
├─ exit 4  → 主机密钥问题：未知→经人类确认后 --yes；变化→告警
├─ exit 3  → 停止。pip install paramiko
├─ exit 124→ 换更长 --timeout，或改为提交作业后轮询
└─ 其他   → 读 stderr：
            "Invalid account or account/partition"  → 检查 --account/--partition
            "QOSNotAllowed" / "QOSMaxWallDuration"  → 检查 --qos/--time
            "unrecognized arguments"                → 本地引号写法错（见 §3.4）
            "No such file or directory"             → 远端路径错（注意 `~` 只在 shell 里展开）
```

---

## 8. 可直接粘贴的系统提示词

```text
你可以通过两个命令行工具操作 HPC 集群（默认：北大未名一号）。

工具：
  hpc-ssh run --json "<远端命令>"        在登录节点执行命令，返回 JSON
  hpc-ssh submit <本地脚本> --remote-dir ~/jobs   上传并提交作业
  hpc-ssh put|get ... --host data        传输文件（首次加 --yes）
  hpc-ssh check --yes                    健康检查

规则（必须遵守）：
1. 远端命令里要用 $USER、$(whoami) 等变量时，整条命令用单引号，避免本地展开。
2. 只依据退出码判断结果：0 成功；其他数字是远端命令的退出码；
   3 缺依赖；4 主机密钥问题；5 网络；6 认证失败；7 无密码；124 超时。
   ——退出码 6 必须立即停止并报告，绝不重试。
3. 登录节点不用于计算。需要算力时写 sbatch 脚本提交，
   必须包含 --account、--partition、--qos；随后用 squeue/sacct 跟踪，用 tail/cat 取日志。
4. 禁止执行破坏性命令（rm -rf、chmod -R、批量删除、修改共享环境）。
5. 禁止读取/打印/上传 cred.dat、密码或 OTP 密钥。
6. 交互式作业用完必须 scancel，否则持续计费。
7. 长任务不要用 run 挂着等；提交作业后轮询。
```

---

## 9. Function calling schema

```json
[
  {
    "name": "hpc_run",
    "description": "在 HPC 集群登录节点上执行一条 shell 命令（只读查询、sbatch、scancel、cat 日志等）。不要用于计算任务本身。",
    "parameters": {
      "type": "object",
      "properties": {
        "command": {"type": "string", "description": "远端 shell 命令。需要远端变量如 $USER 时，调用方不要预先展开。"},
        "host": {"type": "string", "enum": ["login", "data"], "description": "login=提交作业；data=传文件（可联网）"},
        "timeout": {"type": "number", "description": "本地等待秒数，默认 120"}
      },
      "required": ["command"]
    }
  },
  {
    "name": "hpc_submit",
    "description": "把本地 sbatch 脚本上传到集群并提交，返回作业号。",
    "parameters": {
      "type": "object",
      "properties": {
        "script": {"type": "string", "description": "本地脚本路径"},
        "remote_dir": {"type": "string", "description": "远端目录，默认 ~"}
      },
      "required": ["script"]
    }
  },
  {
    "name": "hpc_put",
    "description": "上传本地文件到集群（建议走 data 节点）。",
    "parameters": {
      "type": "object",
      "properties": {
        "local": {"type": "string"},
        "remote": {"type": "string"},
        "yes": {"type": "boolean", "description": "首次连接 data 节点时用于接受主机密钥"}
      },
      "required": ["local", "remote"]
    }
  },
  {
    "name": "hpc_get",
    "description": "从集群下载文件到本地。",
    "parameters": {
      "type": "object",
      "properties": {"remote": {"type": "string"}, "local": {"type": "string"}},
      "required": ["remote", "local"]
    }
  }
]
```

返回值（所有工具统一）：

```json
{"host": "...", "user": "...", "command": "...", "exit_code": 0,
 "timed_out": false, "stdout": "...", "stderr": "..."}
```

---

## 10. 安全边界（部署方须知）

- `cred.dat` 由 Windows DPAPI 加密，**只绑定当前 Windows 用户**：拷到别的机器/用户下无法解密。
  但它仍然等价于"该账号的全部上机权限"，不要放进同步目录、备份或 git。
- OTP 密钥落盘意味着 **MFA 的第二因素变成"本机文件"**。是否允许脚本化登录，
  请先与所在集群的管理方确认（北大：hpc@pku.edu.cn）。
- 建议给 agent 限定工作目录（如 `~/jobs`）并保留人工复核环节；
  最保守的用法是让 agent 只**写脚本**（`put`），由人执行 `submit`。
