"""Run with: $(head -1 $(which ansible-playbook) | sed 's/#!//') -m unittest discover -s tests"""
import json
import os
import random
import sys
import tempfile
import unittest
from collections import Counter

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import cpu_layout  # noqa: E402
import gen_data  # noqa: E402
import gen_queries  # noqa: E402
import parse_dnsperf  # noqa: E402
import plan  # noqa: E402
import process  # noqa: E402
import refpeak  # noqa: E402

# Format from dnsperf 2.16 print_statistics/print_histogram, as in pharos xtask/src/bench.rs.
DNSPERF = """Statistics:

  Queries sent:         102
  Queries completed:    100 (98.04%)
  Queries lost:         2 (1.96%)

  Queries per second:   1234.500000

  Average Latency (s):  0.001000 (min 0.000100, max 0.004000)
  Latency StdDev (s):   0.001000
  Latency bucket (s):   answer count
  0.000100 - 0.000199:  50
  0.001000 - 0.001099:  48
  0.004000 - 0.004099:  2
"""


class Parse(unittest.TestCase):
    def test_percentiles_use_bucket_upper_bounds(self):
        r = parse_dnsperf.parse(DNSPERF)
        self.assertEqual((r["qps"], r["p50"], r["p99"], r["p999"]), (1234.5, 0.000199, 0.004099, 0.004099))
        self.assertAlmostEqual(r["loss_pct"], 1.96, places=1)

    def test_incomplete_histogram_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_dnsperf.parse(DNSPERF.replace("  0.004000 - 0.004099:  2\n", ""))
        with self.assertRaises(ValueError):
            parse_dnsperf.parse("  Queries completed:    0 (0.00%)\n  Queries per second:   0.0\n")


class Generators(unittest.TestCase):
    def gen(self, **kw):
        args = dict(count=20000, seed=1, families=["h{}.z.test,1.0,1000"], zipf_s=1.0, nx=0.0, nx_pattern="",
                    nx_count=100, types=gen_queries.TYPES, hit=None, hot_pool=50, unique_pattern="")
        args.update(kw)
        with tempfile.NamedTemporaryFile("r", suffix=".txt") as f:
            gen_queries.generate(f.name, **args)
            return open(f.name).read().splitlines()

    def test_deterministic_zipf_and_type_mix(self):
        a, b = self.gen(), self.gen()
        self.assertEqual(a, b)
        names = Counter(l.split()[0] for l in a)
        self.assertEqual(names.most_common(1)[0][0], "h1.z.test")
        types = Counter(l.split()[1] for l in a)
        self.assertAlmostEqual(types["A"] / len(a), 0.65, delta=0.03)

    def test_nxdomain_share(self):
        lines = self.gen(nx=0.1, nx_pattern="x{}.z.test")
        self.assertAlmostEqual(sum(l.startswith("x") for l in lines) / len(lines), 0.1, delta=0.02)

    def test_hit_ratio_design(self):
        lines = self.gen(hit=0.5, unique_pattern="u@SALT@x{}.z.test")
        unique = [l for l in lines if l.startswith("u@SALT@")]
        self.assertAlmostEqual(len(unique) / len(lines), 0.5, delta=0.02)
        self.assertEqual(len({l.split()[0] for l in unique}), len(unique))  # never repeated

    def test_hosts_and_zone(self):
        with tempfile.TemporaryDirectory() as d:
            gen_data.main(["hosts", "--count", "3", "--zone", "z.test", "--out", d + "/h"])
            self.assertEqual(len(open(d + "/h").read().splitlines()), 6)
            gen_data.main(["nsd-zone", "--zone", "up.test", "--ns-ip", "10.0.0.1", "--out", d + "/z"])
            self.assertIn("*.h IN A", open(d + "/z").read())


class Layout(unittest.TestCase):
    LSCPU = "# header\n" + "".join(f"{c},{c % 4},0\n" for c in range(8))  # 4 cores, 2 threads each

    def test_one_thread_per_core(self):
        out = cpu_layout.layout(self.LSCPU, {"host": 1, "dut": 3})
        self.assertEqual(out, {"host": [0], "dut": [1, 2, 3]})
        with self.assertRaises(SystemExit):
            cpu_layout.layout(self.LSCPU, {"host": 5})


class RefPeak(unittest.TestCase):
    def test_slowest_server_median(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for server, q in [("a", 100), ("a", 300), ("a", 200), ("b", 50), ("b", 70), ("b", 60)]:
                f.write(json.dumps({"mode": "peak", "proto": "udp", "cores": 4, "server": server, "qps": q}) + "\n")
        self.assertEqual(refpeak.refpeak(f.name, "udp", 4), 60)
        with self.assertRaises(SystemExit):
            refpeak.refpeak(f.name, "tcp", 4)


class Plan(unittest.TestCase):
    def test_smoke_blocks_alternate_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = plan.build(ROOT, "smoke", d)
            self.assertEqual([(b["phase"], b["server"]) for b in p["blocks"]],
                             [("peak", "pharos"), ("peak", "coredns"), ("sweep", "pharos"), ("sweep", "coredns")])
            self.assertTrue(p["blocks"][0]["prepare"] and p["blocks"][-1]["last"])
            self.assertIn("multisocket 4", open(os.path.join(d, p["blocks"][0]["corefile"])).read())

    def test_reps_alternate_server_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = plan.build(ROOT, "whoami", d)
            peak = [b for b in p["blocks"] if b["exp"] == "whoami" and b["phase"] == "peak"]
            self.assertEqual([b["server"] for b in peak], ["pharos", "coredns", "coredns", "pharos"] * 2 + ["pharos", "coredns"])

    def test_every_scenario_plans(self):
        for scene in sorted(os.listdir(os.path.join(ROOT, "scenarios"))):
            if scene.startswith("_"):
                continue
            with tempfile.TemporaryDirectory() as d:
                p = plan.build(ROOT, scene, d)
                self.assertTrue(p["blocks"], scene)
                for e in p["experiments"].values():
                    self.assertIn("dig", e["ready"])

    def test_nomulti_has_no_multisocket(self):
        with tempfile.TemporaryDirectory() as d:
            p = plan.build(ROOT, "whoami", d)
            b = next(b for b in p["blocks"] if b["server"] == "coredns-nomulti")
            self.assertNotIn("multisocket", open(os.path.join(d, b["corefile"])).read())


class Process(unittest.TestCase):
    def test_median_and_bootstrap_ci(self):
        med, lo, hi = process.stats([10, 12, 11, 13, 9])
        self.assertEqual(med, 11)
        self.assertLessEqual(lo, med)
        self.assertGreaterEqual(hi, med)

    def test_pidstat_average(self):
        text = (
            "Average:      UID       PID    %usr %system  %guest   %wait    %CPU   CPU  Command\n"
            "Average:        0       123  250.00   50.00    0.00    1.00  300.00     -  coredns\n\n"
            "Average:      UID       PID  minflt/s  majflt/s     VSZ     RSS   %MEM  Command\n"
            "Average:        0       123      0.50      0.00 1234567  200000   1.00  coredns\n"
        )
        self.assertEqual(process.pidstat_cpu(text), 300.0)

    def test_cache_ratio(self):
        before = 'coredns_cache_hits_total{server="dns://:53",type="success"} 10\ncoredns_cache_misses_total{server="dns://:53"} 10\n'
        after = 'coredns_cache_hits_total{server="dns://:53",type="success"} 110\ncoredns_cache_misses_total{server="dns://:53"} 20\n'
        self.assertAlmostEqual(process.cache_ratio(before, after), 100 / 110)

    def test_empty_target_hit_is_no_target(self):
        rec = {"rundir": "nope", "mode": "peak", "loss_pct": 0.0, "target_hit": ""}
        self.assertTrue(process.enrich("/nonexistent", rec)["valid"])

    def test_end_to_end_marks_bad_cache_ratio_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/loadgen/e")
            with open(f"{d}/loadgen/e/runs.jsonl", "w") as f:
                for rep, hits in ((1, 95), (2, 40)):
                    rundir = f"e/pharos-c4/peak-rep{rep}/udp-peak"
                    os.makedirs(f"{d}/dut/{rundir}")
                    open(f"{d}/dut/{rundir}/metrics.before", "w").write("pharos_cache_hits_total 0\npharos_cache_misses_total 0\n")
                    open(f"{d}/dut/{rundir}/metrics.after", "w").write(f"pharos_cache_hits_total {hits}\npharos_cache_misses_total {100 - hits}\n")
                    open(f"{d}/dut/{rundir}/vmhwm", "w").write("VmHWM:\t  2048 kB\n")
                    f.write(json.dumps({"exp": "e", "server": "pharos", "cores": 4, "proto": "udp", "mode": "peak", "label": "udp-peak",
                                        "rundir": rundir, "target_hit": "0.95", "qps": 1000.0 + rep, "p50": 0.001, "p99": 0.002,
                                        "loss_pct": 0.0}) + "\n")
            process.main(d)
            rows = list(__import__("csv").DictReader(open(f"{d}/summary.csv")))
            self.assertEqual((rows[0]["valid"], rows[0]["total"], rows[0]["rss_kib"]), ("1", "2", "2048"))


if __name__ == "__main__":
    unittest.main()
