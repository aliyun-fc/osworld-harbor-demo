# osworld-harbor-demo

这个仓库把 Harbor 评测任务接到 E2B/FC rund 沙箱后端：

- `RundEnvironment`：rund 沙箱，当前使用杭州端点。

通过 `OSWorldAgent` 运行 `osworld-verified` 数据集。所有扩展都通过
Harbor 的 `--env` 和 `--agent` import path 加载，不需要修改 Harbor 源码。

## 1. 安装

安装 Harbor，并将依赖安装到 Harbor 使用的 Python 环境：

```bash
uv tool install harbor

HARBOR_PY="$HOME/.local/share/uv/tools/harbor/bin/python"
uv pip install --python "$HARBOR_PY" -r requirements.txt

harbor --version
```

插件必须安装到 Harbor 自己的 Python 环境。系统 `python3` 即使能导入本仓库，
也不能证明 Harbor 进程能够导入这些依赖。

## 2. 配置

复制示例配置并填写本地值（`.env` 已被 Git 忽略）：

```bash
cp .env.example .env
```

```dotenv
E2B_API_KEY=...
E2B_API_URL=https://api.<region>.e2b.fc.aliyuncs.com
E2B_DOMAIN=<region>.e2b.fc.aliyuncs.com
E2B_TEMPLATE_IMAGE=fc-e2b-registry.cn-hangzhou.cr.aliyuncs.com/runtime/e2b-dev:osworld-native-dev-v0.0.3-envd
RUND_TEMPLATE=<your-new-rund-template-name>

DASHSCOPE_API_KEY=...
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

这个公网地址是构建模板所需的源镜像，不是可直接复用的 E2B 模板。首次运行
前必须在杭州 rund 后端重新 build，并将新模板名写入 `RUND_TEMPLATE`。旧环境
中的模板名和模板 ID 均不可用。

`E2B_API_KEY`、端点、模板和镜像都是区域资源，必须属于同一区域。不要把真实
key 提交到仓库。

## 3. 运行 osworld-verified

### 3.1 下载与确认路径

```bash
harbor download xlang-ai/osworld-verified -o "$HOME/datasets"
export DATASET="$HOME/datasets/osworld-verified"
```

`--path` 必须指向直接包含 `osworld-verified__...` 任务目录的那一层。不同下载或
解压方式可能多出一层同名目录，运行前先确认：

```bash
find "$DATASET" -maxdepth 2 -name task.toml | head
```

如果输出路径形如 `$DATASET/osworld-verified/osworld-verified__.../task.toml`，
应把 `DATASET` 改成 `$DATASET/osworld-verified`。

### 3.2 单题/小批量

下面是日常调试使用的完整命令。`-i` 先按任务名过滤，`-l` 再取前 N 条；
`-l` 不是随机抽样。

```bash
cd osworld-harbor-demo
set -a; . ./.env; set +a
export PYTHONPATH="$PWD/src:$PWD/vendor/osworld"

harbor run \
  --path "$DATASET" \
  -i '*__os__*' -l 1 \
  --env rund_environment:RundEnvironment \
  --environment-kwarg template="$RUND_TEMPLATE" \
  --environment-kwarg sandbox_timeout_sec=1800 \
  --agent osworld_agent:OSWorldAgent --model qwen3.6-plus \
  --agent-kwarg observation_type=screenshot_a11y_tree \
  --agent-kwarg max_steps=15 \
  --agent-kwarg enable_thinking=false \
  --agent-kwarg gui_only=false \
  --agent-setup-timeout-multiplier 4 \
  --agent-timeout-multiplier 6 \
  --max-retries 1 \
  --n-concurrent 1 --yes
```

调试时保留 `--n-concurrent 1`。确认稳定后再增加并发。

### 3.3 批量跑分

`scripts/run_score.sh` 封装了运行参数。建议先小并发运行，再逐步扩大：

```bash
DATASET="$DATASET" BACKEND=rund MAX_STEPS=15 CONCURRENT=1 \
  bash scripts/run_score.sh
```

长任务可放到后台：

```bash
setsid nohup env DATASET="$DATASET" BACKEND=rund MAX_STEPS=100 CONCURRENT=30 \
  bash scripts/run_score.sh > run_score.log 2>&1 < /dev/null &
```

`run_score.sh` 可通过环境变量覆盖 `BACKEND`、`MODEL`、`MAX_STEPS`、
`CONCURRENT`、`OBS`、`TEMPLATE` 和 `DATASET`。

## 4. 模板管理

公网镜像不能当作模板名直接传给 `harbor run`。首次使用先完成下面的一次性
build；后续 `harbor run` 引用 build 产生的模板名，不要每个 trial 现场 build。

```bash
HARBOR_PY="$HOME/.local/share/uv/tools/harbor/bin/python"
```

### 构建模板

```bash
# build_template.py 会读取 .env，并在 build 后执行 create/kill 探针
set -a; . ./.env; set +a
setsid nohup "$HARBOR_PY" scripts/build_template.py "$RUND_TEMPLATE" \
  > build_rund.log 2>&1 < /dev/null &
tail -f build_rund.log

# 单独检查模板是否已经可以创建沙箱
"$HARBOR_PY" scripts/check_template.py "$RUND_TEMPLATE" --wait
```

构建脚本只有在模板能按名称成功创建并删除探针沙箱后才输出 `READY`。

## 5. 常见问题

### `ConnectError: [Errno 9] Bad file descriptor`

这是 E2B/httpx 数据面或管理面的瞬时连接故障，不代表模型或任务失败。调试命令
不要设置 `--max-retries 0`，建议至少使用 `--max-retries 1`。如果沙箱已经创建，
但第一次 `commands.run` 失败，后面的 `Failed to download logs` 通常只是清理阶段的
连锁提示。

### Harbor 找不到 `rund_environment` 或 `osworld_agent`

确认当前目录和 `PYTHONPATH`：

```bash
cd osworld-harbor-demo
export PYTHONPATH="$PWD/src:$PWD/vendor/osworld"
```

不要把 `cd` 写在 `export PYTHONPATH=...` 的同一条命令里，也不要混入全角标点。

### 数据集显示 0 个任务

`--path` 指错层级。它必须直接包含任务目录；用下面的命令确认：

```bash
find "$DATASET" -maxdepth 2 -name task.toml | head
```

### 模板创建返回 401、403 或区域错误

检查 key、`E2B_API_URL`、`E2B_DOMAIN`、模板和源镜像是否属于同一区域。模板名称
刚 build 完也可能尚未传播，使用 `scripts/check_template.py <name> --wait` 验证。

### 查看失败详情

```bash
harbor view jobs
find jobs/<时间戳> -name exception.txt -o -name trial.log
```

优先查看 trial 的 `exception.txt` 和 `trial.log`，不要只看 job 汇总中的异常类型。

## 6. 代码结构

```text
src/
  rund_environment.py      Harbor -> rund 适配器
  osworld_agent.py         OSWorld GUI agent
  a11y_ref.py              可选的 accessibility 引用编码

vendor/osworld/
  llm_agent.py             OpenAI-compatible 模型客户端
  osworld_e2b.py           沙箱内 OSWorld Flask 客户端
  setup_runner.py          OSWorld 环境初始化步骤

scripts/
  run_score.sh             osworld-verified 批量跑分
  build_template.py        rund 模板构建与验证
  check_template.py        模板别名与 create 探针
```

每个 Harbor trial 独占一个沙箱，流程为：创建沙箱 → agent → verifier → 写入
reward → 删除沙箱。沙箱 ID 和生命周期记录在对应 trial 的日志中。
