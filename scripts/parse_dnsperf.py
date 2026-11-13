#!/usr/bin/env python3
"""Parse dnsperf 2.16 output into JSON (seconds). Same method as pharos xtask/src/bench.rs:
percentiles are nearest-rank estimates using the upper bound of the latency-histogram bucket."""
import json
import re
import sys


def percentiles(buckets, done):
    out = {}
    for name, per_mille in (("p50", 500), ("p90", 900), ("p99", 990), ("p999", 999)):
        rank = -(-done * per_mille // 1000)
        total = 0
        for upper, n in buckets:
            total += n
            if total >= rank:
                out[name] = upper
                break
    return out


def merge(rs, seconds):
    """Combine concurrent dnsperf runs (steady + bursts, parsed with keep_buckets=True) into one record.
    qps is completed queries per measured second of the steady run."""
    done = sum(r["completed"] for r in rs)
    hist = {}
    for r in rs:
        for upper, n in r["buckets"]:
            hist[upper] = hist.get(upper, 0) + n
    sent = sum(r["sent"] for r in rs)
    buckets = sorted(hist.items())
    lost = sum(r.get("lost", 0) for r in rs)
    return {"sent": sent, "completed": done, "lost": lost, "qps": done / seconds, "loss_pct": 100.0 * lost / max(sent, 1),
            "avg": sum(r["avg"] * r["completed"] for r in rs) / done, "min": min(r["min"] for r in rs),
            "max": max(r["max"] for r in rs), **percentiles(buckets, done), "buckets": buckets}


def parse(text, keep_buckets=False):
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
        r.update(percentiles(buckets, done))
    if keep_buckets:
        r["buckets"] = buckets
    return r


if __name__ == "__main__":
    print(json.dumps(parse(sys.stdin.read())))
