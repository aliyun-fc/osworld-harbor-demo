"""prewarm_osworld_cache.py: 预热 osworld-verified 的 setup 素材共享缓存。

扫描本地数据集所有 tests/osworld_task.json 的 download 步骤（253/361 个任务、
447 个文件，全部来自 huggingface/hf-mirror），并发下载到共享缓存目录。
缓存命名与 vendor/osworld/setup_runner.py 完全一致（md5(url)[:10]_basename），
批跑时 SetupRunner 直接命中，setup 阶段不再现场下载。

Usage:
  python scripts/prewarm_osworld_cache.py <osworld-verified-dataset-dir>
  # 缓存目录默认 <repo>/.cache/osworld_setup, 可用 OSWORLD_SETUP_CACHE 覆盖
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("OSWORLD_SETUP_CACHE",
                                REPO / ".cache" / "osworld_setup"))


def _cache_path(url: str, vm_path: str) -> Path:
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    return CACHE_DIR / f"{url_hash}_{os.path.basename(vm_path)}"


def _fetch(url: str, dest: Path) -> str:
    if dest.exists() and dest.stat().st_size > 0:
        return "cached"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    tmp = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return f"{len(data)/1024:.0f}KB"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dataset = Path(sys.argv[1])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    jobs: dict[Path, str] = {}
    for tj in sorted(dataset.glob("*/tests/osworld_task.json")):
        task = json.loads(tj.read_text(encoding="utf-8"))
        for step in task.get("config") or []:
            if step.get("type") != "download":
                continue
            for f in step.get("parameters", {}).get("files", []):
                url, vm_path = f.get("url"), f.get("path")
                if url and vm_path:
                    jobs[_cache_path(url, vm_path)] = url

    print(f"cache dir: {CACHE_DIR}")
    print(f"files:     {len(jobs)} unique", flush=True)
    ok = cached = failed = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_fetch, url, dest): (url, dest)
                for dest, url in jobs.items()}
        for i, fut in enumerate(as_completed(futs), 1):
            url, dest = futs[fut]
            try:
                r = fut.result()
                cached += r == "cached"
                ok += r != "cached"
                if i % 25 == 0 or r != "cached":
                    print(f"[{i}/{len(jobs)}] {r:>7}  {dest.name}", flush=True)
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(jobs)}] FAIL {dest.name}: "
                      f"{type(e).__name__}: {str(e)[:100]}", flush=True)
    print(f"\ndone: downloaded={ok} cached={cached} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
