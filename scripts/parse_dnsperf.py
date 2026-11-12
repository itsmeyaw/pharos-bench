#!/usr/bin/env python3
"""Parse dnsperf 2.16 output into JSON (seconds). Same method as pharos xtask/src/bench.rs:
percentiles are nearest-rank estimates using the upper bound of the latency-histogram bucket."""
import json
import re
import sys


def parse(text):
    r = {}
    buckets = []
    in_hist = False
    for line in (l.strip() for l in text.splitlines()):
        m = re.match(r"Queries sent:\s+(\d+)", line)
        if m:
            r["sent"] = int(m[1])
        m = re.match(r"Queries completed:\s+(\d+)", line)
        if m:
            r["completed"] = int(m[1])
        m = re.match(r"Queries lost:\s+(\d+)", line)
        if m:
            r["lost"] = int(m[1])
        m = re.match(r"Queries per second:\s+([\d.]+)", line)
        if m:
            r["qps"] = float(m[1])
        m = re.match(r"Average Latency \(s\):\s+([\d.]+) \(min ([\d.]+), max ([\d.]+)\)", line)
        if m:
            r["avg"], r["min"], r["max"] = (float(x) for x in m.groups())
        if line == "Latency bucket (s):   answer count":
            in_hist = True
        elif in_hist:
            m = re.match(r"([\d.]+) - ([\d.]+):\s+(\d+)$", line)
            if not m:
                in_hist = False
                continue
            buckets.append((float(m[2]), int(m[3])))
    if not r.get("completed") or not r.get("qps"):
        raise ValueError("dnsperf returned no completed queries")
    done = r["completed"]
    r.setdefault("sent", done + r.get("lost", 0))
    r["loss_pct"] = 100.0 * r.get("lost", 0) / max(r["sent"], 1)
    if done == 1:
        for p in ("p50", "p90", "p99", "p999"):
            r[p] = r["min"]
    else:
        if sum(n for _, n in buckets) != done:
            raise ValueError("dnsperf histogram missing or incomplete (need -O latency-histogram)")
        for name, per_mille in (("p50", 500), ("p90", 900), ("p99", 990), ("p999", 999)):
            rank = -(-done * per_mille // 1000)
            total = 0
            for upper, n in buckets:
                total += n
                if total >= rank:
                    r[name] = upper
                    break
    return r


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read())))
