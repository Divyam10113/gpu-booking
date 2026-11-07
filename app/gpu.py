"""Live GPU telemetry from nvidia-smi, with a mock mode for development.

GPU_MOCK=1               fake data: one process on GPU 0 owned by "bob"
GPU_MOCK="bob@0,carol@1" fake processes owned by these unix users on these GPUs
"""

from __future__ import annotations

import os
import pwd
import random
import subprocess
import time
from dataclasses import asdict, dataclass, field


@dataclass
class Proc:
    pid: int
    user: str
    mem_mib: int
    cmd: str


@dataclass
class GpuInfo:
    index: int
    name: str
    util: int
    mem_used_mib: int
    mem_total_mib: int
    procs: list[Proc] = field(default_factory=list)


@dataclass
class Snapshot:
    taken_at: float
    gpus: list[GpuInfo]
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _smi(*args: str) -> list[list[str]]:
    out = subprocess.run(
        ["nvidia-smi", *args, "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout
    return [[c.strip() for c in line.split(",")] for line in out.splitlines() if line.strip()]


def _owner(pid: int) -> tuple[str, str]:
    """Unix user and command line of a PID, read from /proc."""
    try:
        uid = os.stat(f"/proc/{pid}").st_uid
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        return user, cmd
    except OSError:
        return "?", ""


def _int(s: str) -> int:
    try:
        return int(float(s))
    except ValueError:
        return 0


def read_nvidia_smi() -> list[GpuInfo]:
    gpus: dict[str, GpuInfo] = {}
    for idx, uuid, name, util, used, total in _smi(
        "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total"
    ):
        gpus[uuid] = GpuInfo(int(idx), name, _int(util), _int(used), _int(total))
    for uuid, pid, mem, pname in _smi("--query-compute-apps=gpu_uuid,pid,used_memory,process_name"):
        if uuid not in gpus:
            continue
        user, cmd = _owner(int(pid))
        gpus[uuid].procs.append(Proc(int(pid), user, _int(mem), cmd or pname))
    return sorted(gpus.values(), key=lambda g: g.index)


def read_mock(spec: str, num_gpus: int) -> list[GpuInfo]:
    if spec in ("1", "true", "yes"):
        spec = "bob@0"
    gpus = [GpuInfo(i, "NVIDIA RTX PRO 6000 Blackwell (mock)", 0, 0, 97887) for i in range(num_gpus)]
    for n, item in enumerate(filter(None, (s.strip() for s in spec.split(",")))):
        user, _, idx = item.partition("@")
        g = gpus[int(idx or 0) % num_gpus]
        mem = 20000 + 7000 * n
        g.procs.append(Proc(40000 + n, user, mem, f"python train.py --run {user}-{n}"))
        g.mem_used_mib += mem
        g.util = min(100, g.util + random.randint(55, 95))
    return gpus


def snapshot(num_gpus: int) -> Snapshot:
    mock = os.environ.get("GPU_MOCK", "")
    try:
        gpus = read_mock(mock, num_gpus) if mock else read_nvidia_smi()
        return Snapshot(time.time(), gpus)
    except FileNotFoundError:
        return Snapshot(time.time(), [], "nvidia-smi not found (set GPU_MOCK=1 to test without a GPU)")
    except (subprocess.SubprocessError, ValueError) as e:
        return Snapshot(time.time(), [], f"nvidia-smi failed: {e}")
