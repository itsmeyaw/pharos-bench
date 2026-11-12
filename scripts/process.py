#!/usr/bin/env python3
"""Summarise one results/<run-id>/ directory: summary.csv, summary.md and (if matplotlib is installed) PNGs.

Layout produced by plays/scenario.yml:
  <run>/manifest.json
  <run>/loadgen/<exp>/runs.jsonl                         one record per measured run
  <run>/dut/<exp>/<server>-c<N>/<phase>-rep<R>/<label>/  pidstat.txt, vmhwm, snmp.*, metrics.*
Runs are dropped from the statistics (but counted) when a sub-peak run loses more than 1% of its queries
or the achieved cache hit ratio is more than 5 points from the design target.
"""
import csv
import json
import os
import random
import re
import statistics
import sys
from collections import defaultdict


def stats(values, resamples=2000):
    """Median and bootstrap 95% confidence interval of the median."""
    med = statistics.median(values)
    if len(values) < 2:
        return med, med, med
    rng = random.Random(0)
    meds = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(resamples))
    return med, meds[int(0.025 * resamples)], meds[int(0.975 * resamples) - 1]


def pidstat_cpu(text):
    """%CPU of the 'Average:' row (100 = one core)."""
    header = None
    for line in text.splitlines():
        if not line.startswith("Average:"):
            continue
        cols = line.split()[1:]
        if "%CPU" in cols:
            header = cols
        elif header and len(cols) == len(header):
            return float(cols[header.index("%CPU")])
        else:
            header = None
    return None


def _sum(text, kind):
    total, seen = 0.0, False
    for line in text.splitlines():
        m = re.match(rf"\w+_cache_{kind}_total(?:\{{.*\}})?\s+([\d.e+]+)", line)
        if m:
            total, seen = total + float(m[1]), True
    return total if seen else None


def cache_ratio(before, after):
    h0, h1, m0, m1 = _sum(before, "hits"), _sum(after, "hits"), _sum(before, "misses"), _sum(after, "misses")
    if None in (h0, h1, m0, m1) or (h1 - h0) + (m1 - m0) <= 0:
        return None
    return (h1 - h0) / ((h1 - h0) + (m1 - m0))


def read(path):
    try:
        return open(path).read()
    except OSError:
        return None


def enrich(run_dir, rec):
    probe = os.path.join(run_dir, "dut", rec["rundir"])
    pid = read(os.path.join(probe, "pidstat.txt"))
    rec["cpu_pct"] = pidstat_cpu(pid) if pid else None
    hwm = read(os.path.join(probe, "vmhwm"))
    m = re.search(r"(\d+)", hwm or "")
    rec["rss_kib"] = int(m[1]) if m else None
    b, a = read(os.path.join(probe, "metrics.before")), read(os.path.join(probe, "metrics.after"))
    rec["hit_ratio"] = cache_ratio(b, a) if b is not None and a is not None else None
    rec["valid"], rec["why"] = True, ""
    if rec["mode"] == "sweep" and rec["loss_pct"] > 1.0:
        rec["valid"], rec["why"] = False, f"loss {rec['loss_pct']:.2f}%"
    target = rec.get("target_hit")  # arrives as a string (or "" when the experiment has no design target)
    target = float(target) if target not in (None, "") else None
    if target is not None and (rec["hit_ratio"] is None or abs(rec["hit_ratio"] - target) > 0.05):
        rec["valid"], rec["why"] = False, f"cache hit ratio {rec['hit_ratio']} vs target {target}"
    return rec


def load(run_dir):
    recs = []
    lg = os.path.join(run_dir, "loadgen")
    for exp in sorted(os.listdir(lg)) if os.path.isdir(lg) else []:
        for line in open(os.path.join(lg, exp, "runs.jsonl")):
            recs.append(enrich(run_dir, json.loads(line)))
    return recs


def summarise(recs):
    groups = defaultdict(list)
    for r in recs:
        groups[(r["exp"], r["server"], r["cores"], r["proto"], r["label"])].append(r)
    rows = []
    for (exp, server, cores, proto, label), rs in sorted(groups.items()):
        ok = [r for r in rs if r["valid"]]
        row = {"experiment": exp, "server": server, "cores": cores, "proto": proto, "run": label,
               "valid": len(ok), "total": len(rs)}
        if ok:
            for key in ("qps", "p50", "p99", "loss_pct", "cpu_pct", "rss_kib", "hit_ratio"):
                vals = [r[key] for r in ok if r.get(key) is not None]
                if vals:
                    med, lo, hi = stats(vals)
                    row[key] = med
                    if key in ("qps", "p99"):
                        row[key + "_lo"], row[key + "_hi"] = lo, hi
        rows.append(row)
    return rows


FIELDS = ["experiment", "server", "cores", "proto", "run", "valid", "total", "qps", "qps_lo", "qps_hi",
          "p50", "p99", "p99_lo", "p99_hi", "loss_pct", "cpu_pct", "rss_kib", "hit_ratio"]


def markdown(rows, manifest):
    out = ["# Results", ""]
    if manifest.get("nested"):
        out += ["> **Warning:** nested virtualization host; numbers are not comparable to bare metal.", ""]
    out += [f"Host: `{manifest.get('host_model', '?')}`, kernel `{manifest.get('kernel', '?')}`. "
            f"Pharos `{manifest.get('pharos_sha', '?')}`, CoreDNS `{manifest.get('coredns_version', '?')}`.", "",
            "| experiment | server | cores | run | valid/total | QPS (95% CI) | p50 ms | p99 ms | CPU % | RSS MiB |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "qps" not in r:
            out.append(f"| {r['experiment']} | {r['server']} | {r['cores']} | {r['run']} | 0/{r['total']} | invalid | | | | |")
            continue
        out.append(f"| {r['experiment']} | {r['server']} | {r['cores']} | {r['run']} | {r['valid']}/{r['total']} | "
                   f"{r['qps']:.0f} ({r['qps_lo']:.0f}–{r['qps_hi']:.0f}) | {r['p50'] * 1000:.3f} | "
                   f"{r['p99'] * 1000:.3f} | {r.get('cpu_pct') or 0:.0f} | {(r.get('rss_kib') or 0) / 1024:.0f} |")
    return "\n".join(out) + "\n"


def plots(rows, run_dir):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping plots", file=sys.stderr)
        return
    for exp in sorted({r["experiment"] for r in rows}):
        fig, ax = plt.subplots()
        for server in sorted({r["server"] for r in rows if r["experiment"] == exp}):
            pts = sorted((r["qps"], r["p99"] * 1000) for r in rows
                         if r["experiment"] == exp and r["server"] == server and r["proto"] == "udp" and "qps" in r)
            if pts:
                ax.plot(*zip(*pts), marker="o", label=server)
        ax.set(xlabel="completed QPS", ylabel="p99 latency (ms)", title=exp)
        ax.legend()
        fig.savefig(os.path.join(run_dir, f"{exp}.png"), dpi=120)
        plt.close(fig)


def main(run_dir):
    recs = load(run_dir)
    rows = summarise(recs)
    with open(os.path.join(run_dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    manifest = json.loads(read(os.path.join(run_dir, "manifest.json")) or "{}")
    with open(os.path.join(run_dir, "summary.md"), "w") as f:
        f.write(markdown(rows, manifest))
    plots(rows, run_dir)
    bad = [r for r in recs if not r["valid"]]
    print(f"{len(recs)} runs, {len(bad)} invalid; wrote summary.csv and summary.md in {run_dir}")


if __name__ == "__main__":
    main(sys.argv[1])
