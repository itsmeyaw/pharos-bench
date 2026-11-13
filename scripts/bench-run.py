#!/usr/bin/env python3
"""Run one dnsperf phase on the loadgen. `warmup` discards output; `measure` appends a record to runs.jsonl.
Unique-name workloads (--salted) stream the query file through sed so every run sees fresh names.
With --burst-rate, `measure` also launches a short extra dnsperf every --burst-every seconds; the record merges
the steady run and all bursts (one latency histogram), so tail percentiles include the burst queries."""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_dnsperf import merge, parse  # noqa: E402


def run(a, seconds, rate, salt, out):
    cmd = ["dnsperf", "-s", a.server_ip, "-p", str(a.port), "-m", a.proto, "-e", "-c", str(a.clients),
           "-T", str(a.threads), "-q", str(a.inflight), "-l", str(seconds), "-O", "latency-histogram"]
    if rate:
        cmd += ["-Q", str(rate)]
    with open(out + ".out", "w") as o, open(out + ".err", "w") as e:
        if a.salted:
            sed = subprocess.Popen(["sed", f"s/@SALT@/{salt}/", a.queries], stdout=subprocess.PIPE)
            p = subprocess.run(cmd, stdin=sed.stdout, stdout=o, stderr=e)
            sed.stdout.close()
            sed.wait()
        else:
            p = subprocess.run(cmd + ["-d", a.queries], stdout=o, stderr=e)
    if p.returncode:
        raise RuntimeError(f"dnsperf failed ({p.returncode}): {open(out + '.err').read()}")


def bursts(a, salt, out, done, errors):
    t0 = time.time()
    n = 1
    while n * a.burst_every + a.burst_secs <= a.seconds:
        time.sleep(max(t0 + n * a.burst_every - time.time(), 0))
        try:
            run(a, a.burst_secs, a.burst_rate, f"{salt}b{n}", f"{out}.burst{n}")
            done.append(f"{out}.burst{n}.out")
        except RuntimeError as e:
            errors.append(e)
        n += 1


def measure(a, salt, out):
    if not a.burst_rate:
        run(a, a.seconds, a.rate, salt, out)
        return parse(open(out + ".out").read(), keep_buckets=True)
    done, errors = [], []
    t = threading.Thread(target=bursts, args=(a, salt, out, done, errors))
    t.start()
    try:
        run(a, a.seconds, a.rate, salt, out)
    finally:
        t.join()
    if errors:
        raise errors[0]
    rec = merge([parse(open(f).read(), keep_buckets=True) for f in [out + ".out"] + done], a.seconds)
    rec["bursts"] = len(done)
    return rec


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=["warmup", "measure"])
    p.add_argument("--server-ip", required=True)
    p.add_argument("--port", type=int, default=53)
    p.add_argument("--proto", choices=["udp", "tcp"], required=True)
    p.add_argument("--rate", type=int, default=0, help="queries/s; 0 = closed-loop peak")
    p.add_argument("--burst-rate", type=int, default=0, help="extra queries/s during each burst (measure only)")
    p.add_argument("--burst-secs", type=int, default=1)
    p.add_argument("--burst-every", type=int, default=10)
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
    if a.rate or a.burst_rate:
        a.inflight = max(a.inflight, 4096)  # keep -q from throttling a rate-limited run
    salt = f"{a.phase[0]}{int(time.time())}"
    out = a.out + "." + a.phase
    try:
        if a.phase == "warmup":
            run(a, a.seconds, a.rate, salt, out)
            return
        rec = {**json.loads(a.meta), "rate": a.rate, "burst_rate": a.burst_rate, **measure(a, salt, out)}
    except RuntimeError as e:
        sys.exit(str(e))
    with open(a.runs_jsonl, "a") as f:
        f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
