#!/bin/bash
# 全量 osworld-verified(361 题) 跑分 —— micro / rund 双后端统一入口
#
# 用法:
#   bash scripts/run_score.sh                        # micro, 100步, 50并发(默认)
#   BACKEND=rund bash scripts/run_score.sh           # 换 rund 后端
#   MAX_STEPS=15 CONCURRENT=30 bash scripts/run_score.sh   # 时间路线(15步)
#   MODEL=qwen3.7-plus bash scripts/run_score.sh     # 换模型
#
# 建议后台执行(整轮数小时, SSH 断连会中断):
#   setsid nohup bash scripts/run_score.sh > run_score.log 2>&1 < /dev/null &
#
# ── 可调变量 ────────────────────────────────────────────────────────────
#   BACKEND     micro | rund          默认 micro
#   MODEL       模型名                默认 qwen3.6-plus
#   MAX_STEPS   单题步数上限          默认 100 (时间路线用 15)
#   CONCURRENT  并发 trial 数         默认 50
#   OBS         observation_type      默认 screenshot_a11y_tree
#   TEMPLATE    模板名                默认读取 RUND_TEMPLATE / MICRO_TEMPLATE
#   DATASET     数据集路径            默认 ../osworld-verified/osworld-verified
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BACKEND="${BACKEND:-micro}"
MODEL="${MODEL:-qwen3.6-plus}"
MAX_STEPS="${MAX_STEPS:-100}"
CONCURRENT="${CONCURRENT:-50}"
OBS="${OBS:-screenshot_a11y_tree}"
DATASET="${DATASET:-$REPO/../osworld-verified/osworld-verified}"

case "$BACKEND" in
  micro)
    ENV_FILE=".env.micro"
    ENV_IMPL="micro_environment:MicroEnvironment"
    # micro 上海镜像: OSWorld Flask 由 systemd 自启在 5000; 官方 server 无 /health;
    # 且镜像无 ImageMagick(import 截图不可用) -> 截图走 flask /screenshot
    BACKEND_ARGS=(
      --agent-kwarg flask_port=5000
      --agent-kwarg bootstrap=false
      --agent-kwarg ready_probe=screenshot
      --agent-kwarg screenshot_via=flask
    )
    ;;
  rund)
    ENV_FILE=".env"
    ENV_IMPL="rund_environment:RundEnvironment"
    # rund 镜像: entrypoint.sh 引导 + OSWorld server 在 8081, 走默认 agent 参数
    BACKEND_ARGS=()
    ;;
  *)
    echo "ERROR: BACKEND 只能是 micro 或 rund (当前: $BACKEND)" >&2
    exit 1
    ;;
esac

[ -f "$ENV_FILE" ] || { echo "ERROR: 缺少 $ENV_FILE" >&2; exit 1; }
set -a; . "./$ENV_FILE"; set +a
if [ -z "${TEMPLATE:-}" ]; then
  if [ "$BACKEND" = "micro" ]; then
    TEMPLATE="${MICRO_TEMPLATE:-}"
  else
    TEMPLATE="${RUND_TEMPLATE:-}"
  fi
fi
[ -n "$TEMPLATE" ] || {
  echo "ERROR: 请设置 TEMPLATE，或在 env 文件中设置对应的 RUND_TEMPLATE/MICRO_TEMPLATE" >&2
  exit 1
}
export PYTHONPATH="$REPO/src:$REPO/vendor/osworld"

echo "backend=$BACKEND  template=$TEMPLATE  model=$MODEL"
echo "obs=$OBS  max_steps=$MAX_STEPS  concurrent=$CONCURRENT"
echo "dataset=$DATASET"

exec harbor run \
  --path "$DATASET" \
  --env "$ENV_IMPL" \
  --environment-kwarg "template=$TEMPLATE" \
  --environment-kwarg sandbox_timeout_sec=7200 \
  --agent osworld_agent:OSWorldAgent --model "$MODEL" \
  --agent-kwarg "observation_type=$OBS" \
  --agent-kwarg "max_steps=$MAX_STEPS" \
  --agent-kwarg enable_thinking=false \
  --agent-kwarg gui_only=false \
  "${BACKEND_ARGS[@]}" \
  --agent-setup-timeout-multiplier 4 \
  --agent-timeout-multiplier 6 \
  --max-retries 1 \
  --n-concurrent "$CONCURRENT" --yes
