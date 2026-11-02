# 用途：板上正式运行入口骨架。固定输入，测单帧耗时，保存结果供离线比对。
# 输入：固定输入样本路径、运行次数、预热次数。
# 输出：results.json，含每帧耗时、中位数、P95、以及边界标签。
# 工具版本：PYNQ 2.7 与 3.x，Python 3.8 及以上。
# 未验证项：DMA 搬运部分未在此骨架中实现，接上自己的驱动后必须重测边界。

import json
import math
import statistics
import sys
import time
from pathlib import Path
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    # keep Chinese readable when output is captured by another tool
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPEATS = 30
WARMUP = 5
BOUNDARY = "accel-only/fixed-input/includes-transfer/no-display/median"


def run_once() -> float:
    # TODO: 这里接你的推理或搬运调用，返回单帧耗时（秒）
    raise NotImplementedError("connect your accelerator call here")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        print("NOT_RUN: accelerator not connected, boundary spec only")
        print(json.dumps({"boundary": BOUNDARY, "repeats": REPEATS, "warmup": WARMUP}))
        return 3

    for _ in range(WARMUP):
        run_once()

    samples = [run_once() for _ in range(REPEATS)]
    samples_sorted = sorted(samples)
    # 最近秩法：第 ceil(0.95*n) 个样本。30 个样本时取第 29 个（下标 28），
    # 写成 int(n*0.95)-1 会落到下标 27，实际报的是 P93，比自称的 P95 乐观。
    p95_rank = math.ceil(0.95 * len(samples_sorted))
    p95 = samples_sorted[max(0, p95_rank - 1)]

    result = {
        "boundary": BOUNDARY,
        "repeats": REPEATS,
        "warmup": WARMUP,
        "median_ms": round(statistics.median(samples) * 1000, 3),
        "p95_ms": round(p95 * 1000, 3),
        "min_ms": round(min(samples) * 1000, 3),
        "max_ms": round(max(samples) * 1000, 3),
    }
    out = Path(__file__).resolve().parent / "results.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
