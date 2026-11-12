#!/usr/bin/env python3
"""Assign one hardware thread per physical core: cpu_layout.py host=4 dut=8 loadgen=16 backend=8
Reads `lscpu -p=CPU,CORE,SOCKET` from stdin (or runs lscpu); prints JSON {name: [cpu ids]}.
Hyperthread siblings stay unused so VMs never share a core."""
import json
import subprocess
import sys


def layout(lscpu_text, wanted):
    first = {}
    for line in lscpu_text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        cpu, core, socket = (int(x) for x in line.split(",")[:3])
        key = (socket, core)
        first[key] = min(first.get(key, cpu), cpu)
    cores = [first[k] for k in sorted(first)]
    need = sum(wanted.values())
    if need > len(cores):
        raise SystemExit(f"need {need} physical cores, host has {len(cores)}")
    out, i = {}, 0
    for name, n in wanted.items():
        out[name] = cores[i:i + n]
        i += n
    return out


if __name__ == "__main__":
    wanted = {k: int(v) for k, v in (a.split("=") for a in sys.argv[1:])}
    text = sys.stdin.read() if not sys.stdin.isatty() else subprocess.check_output(["lscpu", "-p=CPU,CORE,SOCKET"], text=True)
    print(json.dumps(layout(text, wanted)))
