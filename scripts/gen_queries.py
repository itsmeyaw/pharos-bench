#!/usr/bin/env python3
"""Deterministic dnsperf query file: Zipf name popularity, type mix, NXDOMAIN share, and an optional
cache-hit-ratio design (hot pool vs. never-repeated names carrying an @SALT@ placeholder that
bench-run.py replaces per run so every run sees fresh names)."""
import argparse
import random
from itertools import accumulate

TYPES = "A=65,AAAA=25,TXT=4,MX=3,SRV=3"


def zipf(rng, n, s):
    cum = list(accumulate(1.0 / i**s for i in range(1, n + 1)))
    ranks = range(1, n + 1)
    return lambda k: rng.choices(ranks, cum_weights=cum, k=k)


def generate(out, count, seed, families, zipf_s, nx, nx_pattern, nx_count, types, hit, hot_pool, unique_pattern, ndots_search=()):
    rng = random.Random(seed)
    tnames, tweights = zip(*((t, float(w)) for t, w in (x.split("=") for x in types.split(","))))
    fams = []
    for spec in families:
        pattern, weight, n = spec.rsplit(",", 2)
        fams.append((pattern, float(weight), zipf(rng, int(n), zipf_s)))
    nx_draw = zipf(rng, nx_count, zipf_s) if nx > 0 else None
    hot_pattern = fams[0][0]
    unique = 0
    chunk = 100_000
    amp = 2 * (len(ndots_search) + 1) if ndots_search else 1  # lines per logical lookup
    with open(out, "w") as f:
        left = count
        while left > 0:
            k = max(min(chunk, left) // amp, 1)
            left -= k * amp
            kinds = rng.choices(range(len(fams)), weights=[x[1] for x in fams], k=k)
            draws = [fams[i][2](kinds.count(i)) for i in range(len(fams))]
            qtypes = rng.choices(tnames, weights=tweights, k=k)
            lines = []
            for j in range(k):
                if hit is not None:
                    if rng.random() < hit:
                        name = hot_pattern.format(rng.randint(1, hot_pool))
                    else:
                        unique += 1
                        name = unique_pattern.format(unique)
                elif nx and rng.random() < nx:
                    name = nx_pattern.format(nx_draw(1)[0])
                else:
                    i = kinds[j]
                    name = fams[i][0].format(draws[i].pop())
                if ndots_search:
                    # a stub resolver with ndots:5 tries every search suffix (A+AAAA, all NXDOMAIN) before the real name
                    lines += [f"{n} {t}\n" for n in [f"{name}.{s}" for s in ndots_search] + [name] for t in ("A", "AAAA")]
                else:
                    lines.append(f"{name} {qtypes[j]}\n")
            f.writelines(lines)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--family", action="append", required=True, help="PATTERN,WEIGHT,COUNT; pattern uses {}")
    p.add_argument("--zipf", type=float, default=1.0)
    p.add_argument("--nx", type=float, default=0.0)
    p.add_argument("--nx-pattern", default="")
    p.add_argument("--nx-count", type=int, default=100000)
    p.add_argument("--types", default=TYPES)
    p.add_argument("--hit", type=float)
    p.add_argument("--hot-pool", type=int, default=5000)
    p.add_argument("--unique-pattern", default="")
    p.add_argument("--ndots-search", default="", help="comma-separated search suffixes; each name becomes a ndots-style lookup")
    a = p.parse_args(argv)
    if a.hit is not None and "@SALT@" not in a.unique_pattern:
        p.error("--hit needs --unique-pattern containing @SALT@")
    if a.nx and not a.nx_pattern:
        p.error("--nx needs --nx-pattern")
    generate(a.out, a.count, a.seed, a.family, a.zipf, a.nx, a.nx_pattern, a.nx_count, a.types,
             a.hit, a.hot_pool, a.unique_pattern, [s for s in a.ndots_search.split(",") if s])


if __name__ == "__main__":
    main()
