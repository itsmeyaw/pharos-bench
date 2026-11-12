#!/usr/bin/env python3
"""Run one dnsperf phase on the loadgen. `warmup` discards output; `measure` appends a record to runs.jsonl.
Unique-name workloads (--salted) stream the query file through sed so every run sees fresh names."""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_dnsperf import parse  # noqa: E402


def run(a, seconds, salt, out):
    cmd = ["dnsperf", "-s", a.server_ip, "-p", str(a.port), "-m", a.proto, "-e", "-c", str(a.clients),
           "-T", str(a.threads), "-q", str(a.inflight), "-l", str(seconds), "-O", "latency-histogram"]
    if a.rate:
        cmd += ["-Q", str(a.rate)]
    with open(out + ".out", "w") as o, open(out + ".err", "w") as e:
        if a.salted:
            sed = subprocess.Popen(["sed", f"s/@SALT@/{salt}/", a.queries], stdout=subprocess.PIPE)
            p = subprocess.run(cmd, stdin=sed.stdout, stdout=o, stderr=e)
            sed.stdout.close()
            sed.wait()
        else:
            p = subprocess.run(cmd + ["-d", a.queries], stdout=o, stderr=e)
    if p.returncode:
        sys.exit(f"dnsperf failed ({p.returncode}): {open(out + '.err').read()}")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=["warmup", "measure"])
    p.add_argument("--server-ip", required=True)
    p.add_argument("--port", type=int, default=53)
    p.add_argument("--proto", choices=["udp", "tcp"], required=True)
    p.add_argument("--rate", type=int, default=0, help="queries/s; 0 = closed-loop peak")
    p.add_argument("--seconds", type=int, required=True)
    p.add_argument("--queries", required=True)
    p.add_argument("--salted", action="store_true")
    p.add_argument("--out", required=True, help="output prefix")
    p.add_argument("--meta", default="{}")
    p.add_argument("--runs-jsonl")
    p.add_argument("--clients", type=int, default=64)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--inflight", type=int, default=512)
    a = p.parse_args(argv)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    if a.rate:
        a.inflight = max(a.inflight, 4096)  # keep -q from throttling a rate-limited run
    salt = f"{a.phase[0]}{int(time.time())}"
    run(a, a.seconds, salt, a.out + "." + a.phase)
    if a.phase == "measure":
        rec = {**json.loads(a.meta), "rate": a.rate,
               **parse(open(a.out + ".measure.out").read())}
        with open(a.runs_jsonl, "a") as f:
            f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
