#!/usr/bin/env python3
"""refpeak.py RUNS_JSONL PROTO CORES -> the slowest server's median peak QPS (integer).
Sweep rates are fractions of this, so both servers face the same absolute offered load."""
import json
import statistics
import sys


def refpeak(path, proto, cores):
    qps = {}
    for line in open(path):
        r = json.loads(line)
        if r["mode"] == "peak" and r["proto"] == proto and int(r["cores"]) == cores:
            qps.setdefault(r["server"], []).append(r["qps"])
    if len(qps) < 2:
        raise SystemExit(f"need peak runs of at least two servers in {path} ({proto}, {cores} cores); found {sorted(qps)}")
    return int(min(statistics.median(v) for v in qps.values()))


if __name__ == "__main__":
    print(refpeak(sys.argv[1], sys.argv[2], int(sys.argv[3])))
