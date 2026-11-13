#!/usr/bin/env python3
"""Generate server-side fixtures: hosts file, NSD zone/config, kubeconfig."""
import argparse
import json


def hosts(a):
    with open(a.out, "w") as f:
        for i in range(1, a.count + 1):
            f.write(f"10.{i >> 16 & 255}.{i >> 8 & 255}.{i & 255} h{i}.{a.zone}\n")
            f.write(f"fd00::{i >> 16:x}:{i & 0xffff:x} h{i}.{a.zone}\n")


def nsd_zone(a):
    z, t = a.zone, a.ttl
    with open(a.out, "w") as f:
        f.write(f"$ORIGIN {z}.\n$TTL {t}\n")
        f.write(f"@ IN SOA ns admin 1 3600 600 86400 {t}\n@ IN NS ns\nns IN A {a.ns_ip}\n")
        # One wildcard under h.<zone> answers every positive name; names outside it are NXDOMAIN (RFC 4592).
        f.write(f"*.h IN A 192.0.2.1\n*.h IN AAAA 2001:db8::1\n*.h IN TXT \"pharos-bench\"\n")
        f.write("*.h IN MX 10 mail.h\n*.h IN SRV 0 5 80 target.h\n")


def nsd_conf(a):
    with open(a.out, "w") as f:
        f.write(
            "server:\n  ip-address: 0.0.0.0\n  server-count: 4\n  hide-version: yes\n  verbosity: 0\n  rrl-ratelimit: 0\n"
            + (f"  tcp-count: {a.tcp_count}\n" if a.tcp_count else "")
            + f'zone:\n  name: "{a.zone}"\n  zonefile: "{a.zonefile}"\n'
        )


def kubeconfig(a):
    cfg = {
        "apiVersion": "v1", "kind": "Config",
        "clusters": [{"name": "bench", "cluster": {"server": a.server}}],
        "contexts": [{"name": "bench", "context": {"cluster": "bench", "user": "bench"}}],
        "current-context": "bench", "users": [{"name": "bench", "user": {}}],
    }
    with open(a.out, "w") as f:
        json.dump(cfg, f)


def main(argv=None):
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)
    h = s.add_parser("hosts"); h.add_argument("--count", type=int, required=True)
    h.add_argument("--zone", required=True); h.add_argument("--out", required=True); h.set_defaults(f=hosts)
    z = s.add_parser("nsd-zone"); z.add_argument("--zone", required=True); z.add_argument("--out", required=True)
    z.add_argument("--ttl", type=int, default=300); z.add_argument("--ns-ip", required=True); z.set_defaults(f=nsd_zone)
    c = s.add_parser("nsd-conf"); c.add_argument("--zone", required=True); c.add_argument("--zonefile", required=True)
    c.add_argument("--out", required=True); c.add_argument("--tcp-count", type=int); c.set_defaults(f=nsd_conf)
    k = s.add_parser("kubeconfig"); k.add_argument("--server", required=True); k.add_argument("--out", required=True)
    k.set_defaults(f=kubeconfig)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
