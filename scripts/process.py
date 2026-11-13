#!/usr/bin/env python3
"""Summarise one results/<run-id>/ directory: summary.csv, summary.md and (if matplotlib is installed) PNGs.

Layout produced by plays/scenario.yml:
  <run>/manifest.json
  <run>/loadgen/<exp>/runs.jsonl                         one record per measured run
  <run>/dut/<exp>/<server>-c<N>/<phase>-rep<R>/<label>/  pidstat.txt, vmhwm, snmp.*, metrics.*
Per repetition: runs.csv (one row per measured run, for box plots) and latency_hist.csv (one row per histogram
bucket per run, for CDFs). Runs are dropped from the statistics (but counted) when a sub-peak run loses more than 1% of its queries
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


def _sum(text, kind, typ=None):
    """Sum of *_cache_<kind>_total samples; with `typ` only samples labelled type="<typ>" (0 if none yet)."""
    total, seen = 0.0, False
    for line in text.splitlines():
        m = re.match(rf"\w+_cache_{kind}_total(?:\{{(.*)\}})?\s+([\d.e+]+)", line)
        if m:
            seen = True
            if typ is None or f'type="{typ}"' in (m[1] or ""):
                total += float(m[2])
    return total if seen else None


def cache_ratio(before, after, typ=None):
    """Cache hits (only those of `typ` if given, e.g. denial = negative-cache hits) as a share of all lookups."""
    h0, h1, m0, m1 = _sum(before, "hits", typ), _sum(after, "hits", typ), _sum(before, "misses"), _sum(after, "misses")
    a0, a1 = _sum(before, "hits"), _sum(after, "hits")
    if None in (h0, h1, m0, m1, a0, a1) or (a1 - a0) + (m1 - m0) <= 0:
        return None
    return (h1 - h0) / ((a1 - a0) + (m1 - m0))


def mpstat_busy(text):
    """Whole-VM CPU busy % (100 - %idle) from the 'Average:' row."""
    cols = None
    for line in text.splitlines():
        f = line.split()
        if "%idle" in f:
            cols = f
        elif cols and f and f[0] == "Average:" and len(f) == len(cols):
            return 100.0 - float(f[-1])
    return None


def read(path):
    try:
        return open(path).read()
    except OSError:
        return None


def enrich(run_dir, rec):
    probe = os.path.join(run_dir, "dut", rec["rundir"])
    pid = read(os.path.join(probe, "pidstat.txt"))
    rec["cpu_pct"] = pidstat_cpu(pid) if pid else None
    for node in ("loadgen", "backend"):
        m = read(os.path.join(run_dir, node, rec["rundir"], "mpstat.txt"))
        rec[node + "_cpu"] = mpstat_busy(m) if m else None
    hwm = read(os.path.join(probe, "vmhwm"))
    m = re.search(r"(\d+)", hwm or "")
    rec["rss_kib"] = int(m[1]) if m else None
    b, a = read(os.path.join(probe, "metrics.before")), read(os.path.join(probe, "metrics.after"))
    rec["hit_ratio"] = cache_ratio(b, a) if b is not None and a is not None else None
    rec["denial_share"] = cache_ratio(b, a, "denial") if b is not None and a is not None else None
    if rec.get("qps"):
        rec["lookups"] = rec["qps"] / int(rec.get("amp") or 1)
    rec["valid"], rec["why"] = True, ""
    if rec["mode"] == "sweep" and rec["loss_pct"] > 1.0 and not rec.get("burst"):  # bursts may overload on purpose
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
        path = os.path.join(lg, exp, "runs.jsonl")
        if os.path.exists(path):  # absent when every block of the experiment failed to start (e.g. OOM)
            recs += [enrich(run_dir, json.loads(line)) for line in open(path)]
    return recs


def load_startup(run_dir):
    """One row per block from dut/<exp>/<server>-c<N>/<phase>-rep<R>/startup (written by `bench-probe.sh sync`)."""
    dut = os.path.join(run_dir, "dut")
    rows = []
    for d, _, files in sorted(os.walk(dut)):
        if "startup" not in files:
            continue
        exp, srv, _blk = os.path.relpath(d, dut).split(os.sep)
        kv = {k: int(v) for k, v in re.findall(r"(\w+)\s*[=:]\s*(\d+)", open(os.path.join(d, "startup")).read())}
        server, cores = srv.rsplit("-c", 1)
        rows.append({"experiment": exp, "server": server, "cores": cores, "ready": kv.get("ready", 0),
                     "sync_hwm_kib": kv.get("VmHWM"), "sync_rss_kib": kv.get("VmRSS"), "oom_kills": kv.get("oom_kill", 0)})
    return rows


def summarise_startup(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["experiment"], r["server"], r["cores"])].append(r)
    out = []
    for (exp, server, cores), rs in sorted(groups.items()):
        row = {"experiment": exp, "server": server, "cores": cores, "ready": sum(r["ready"] for r in rs), "total": len(rs),
               "oom_kills": sum(r["oom_kills"] for r in rs)}
        for key in ("sync_hwm_kib", "sync_rss_kib"):
            vals = [r[key] for r in rs if r[key] is not None]
            if vals:
                row[key] = statistics.median(vals)
        out.append(row)
    return out


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
            for key in ("qps", "p50", "p99", "p999", "max", "loss_pct", "cpu_pct", "rss_kib", "hit_ratio", "denial_share",
                        "lookups", "loadgen_cpu", "backend_cpu"):
                vals = [r[key] for r in ok if r.get(key) is not None]
                if vals:
                    med, lo, hi = stats(vals)
                    row[key] = med
                    if key in ("qps", "p99", "p999"):
                        row[key + "_lo"], row[key + "_hi"] = lo, hi
        rows.append(row)
    return rows


FIELDS = ["experiment", "server", "cores", "proto", "run", "valid", "total", "qps", "qps_lo", "qps_hi",
          "p50", "p99", "p99_lo", "p99_hi", "p999", "p999_lo", "p999_hi", "max", "loss_pct", "cpu_pct", "rss_kib",
          "hit_ratio", "denial_share", "lookups", "loadgen_cpu", "backend_cpu"]
RUN_FIELDS = ["exp", "server", "cores", "proto", "label", "phase", "rep", "mode", "frac", "rate", "burst_rate", "valid", "why",
              "sent", "completed", "lost", "loss_pct", "qps", "lookups", "avg", "min", "p50", "p90", "p99", "p999", "max",
              "cpu_pct", "rss_kib", "hit_ratio", "denial_share", "loadgen_cpu", "backend_cpu"]
HIST_FIELDS = ["exp", "server", "cores", "proto", "label", "phase", "rep", "valid", "upper_s", "count", "cum_frac"]
STARTUP_FIELDS = ["experiment", "server", "cores", "ready", "total", "oom_kills", "sync_hwm_kib", "sync_rss_kib"]


def hist_rows(recs):
    """Long-format latency histogram: cum_frac is the empirical CDF at the bucket's upper bound."""
    for r in recs:
        total = sum(n for _, n in r.get("buckets", []))
        cum = 0
        for upper, n in r.get("buckets", []):
            cum += n
            yield {**{k: r.get(k) for k in HIST_FIELDS[:8]}, "upper_s": upper, "count": n, "cum_frac": cum / total}


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def ms(r, key):
    return f"{r[key] * 1000:.3f}" if key in r else ""


def pct(r, key):
    return f"{r[key] * 100:.0f}" if key in r else ""


def markdown(rows, startup, manifest):
    out = ["# Results", ""]
    if manifest.get("nested"):
        out += ["> **Warning:** nested virtualization host; numbers are not comparable to bare metal.", ""]
    out += [f"Host: `{manifest.get('host_model', '?')}`, kernel `{manifest.get('kernel', '?')}`. "
            f"Pharos `{manifest.get('pharos_sha', '?')}`, CoreDNS `{manifest.get('coredns_version', '?')}`.", "",
            "| experiment | server | cores | run | valid/total | QPS (95% CI) | p50 ms | p99 ms | p99.9 ms | max ms | loss % | "
            "denial hit % | lookups/s | CPU % | RSS MiB | loadgen / backend VM busy % |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        head = f"| {r['experiment']} | {r['server']} | {r['cores']} | {r['run']} | "
        if "qps" not in r:
            out.append(head + f"0/{r['total']} | invalid |" + " |" * 10)
            continue
        out.append(head + f"{r['valid']}/{r['total']} | {r['qps']:.0f} ({r['qps_lo']:.0f}–{r['qps_hi']:.0f}) | "
                   f"{ms(r, 'p50')} | {ms(r, 'p99')} | {ms(r, 'p999')} | {ms(r, 'max')} | {r.get('loss_pct', 0):.2f} | "
                   f"{pct(r, 'denial_share')} | {r.get('lookups', 0):.0f} | {r.get('cpu_pct') or 0:.0f} | "
                   f"{(r.get('rss_kib') or 0) / 1024:.0f} | "
                   f"{r.get('loadgen_cpu', float('nan')):.0f} / {r.get('backend_cpu', float('nan')):.0f} |")
    if startup:
        out += ["", "## Startup (median over blocks; memory is measured right after the initial sync, before any load)", "",
                "| experiment | server | cores | ready/blocks | OOM kills | peak RSS MiB | RSS MiB |", "|---|---|---|---|---|---|---|"]
        for s in startup:
            mib = lambda k: f"{s[k] / 1024:.0f}" if k in s else ""  # noqa: E731
            out.append(f"| {s['experiment']} | {s['server']} | {s['cores']} | {s['ready']}/{s['total']} | {s['oom_kills']} | "
                       f"{mib('sync_hwm_kib')} | {mib('sync_rss_kib')} |")
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
    startup = summarise_startup(load_startup(run_dir))
    with open(os.path.join(run_dir, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    if startup:
        with open(os.path.join(run_dir, "startup.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, STARTUP_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(startup)
    write_csv(os.path.join(run_dir, "runs.csv"), RUN_FIELDS, recs)
    write_csv(os.path.join(run_dir, "latency_hist.csv"), HIST_FIELDS, hist_rows(recs))
    manifest = json.loads(read(os.path.join(run_dir, "manifest.json")) or "{}")
    with open(os.path.join(run_dir, "summary.md"), "w") as f:
        f.write(markdown(rows, startup, manifest))
    plots(rows, run_dir)
    bad = [r for r in recs if not r["valid"]]
    print(f"{len(recs)} runs, {len(bad)} invalid; wrote summary.csv, runs.csv, latency_hist.csv and summary.md in {run_dir}")


if __name__ == "__main__":
    main(sys.argv[1])
