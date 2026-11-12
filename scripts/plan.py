#!/usr/bin/env python3
"""Expand a scenario (five YAML files, EnGINE style) into build/<scene>/plan.json plus rendered Corefiles.

Ansible then only loops over plan['blocks']. Corefiles are written to disk, not passed through
Ansible variables, because Go templates in them contain `{{` which Ansible would try to template.
Needs PyYAML (Ansible's own python has it): run with {{ ansible_playbook_python }}.
"""
import argparse
import json
import os
import shlex
import sys

import yaml

FILES = ["00-nodes", "01-network", "02-stacks", "03-actions", "04-experiments"]
LIB = "/usr/local/lib/bench"
DATA = "/var/lib/bench"
UNIT = "dns-bench"


def read(root, scene, name):
    for d in (scene, "_defaults"):
        path = os.path.join(root, "scenarios", d, name + ".yml")
        if os.path.exists(path):
            return yaml.safe_load(open(path)) or {}
    return {}


def load(root, scene):
    s = {n: read(root, scene, n) for n in FILES}
    base = read(root, "_defaults", "02-stacks")
    stacks, servers = dict(base.get("stacks", {})), dict(base.get("servers", {}))
    for other in s["04-experiments"].get("uses", []) + [scene]:
        d = read(root, other, "02-stacks")
        stacks.update(d.get("stacks", {}))
        servers.update(d.get("servers", {}))
    s["stacks"], s["servers"] = stacks, servers
    return s


def render_corefile(text, server, cores, upstream, kubeconfig):
    multisocket = f"multisocket {cores}" if server["multisocket"] else ""
    for k, v in {"@MULTISOCKET@": multisocket, "@UPSTREAM@": upstream, "@KUBECONFIG@": kubeconfig}.items():
        text = text.replace(k, v)
    return text


def services(stack, node):
    return sorted(stack.get("services", {}).get(node, []), key=lambda s: s.get("level", 0))


def queries_command(exp, q, warmup, duration):
    salted = "hit" in q
    count = int(exp.get("max_qps", 250000)) * max(warmup, duration) if salted else int(q.get("count", 2_000_000))
    args = [f"--out {DATA}/queries/{exp['name']}.txt", f"--count {count}", f"--seed {q.get('seed', 1)}",
            f"--zipf {q.get('zipf', 1.0)}"]
    for fam in q["families"]:
        args.append(f"--family {shlex.quote(fam)}")
    if q.get("nx"):
        args += [f"--nx {q['nx']}", f"--nx-pattern {shlex.quote(q['nx_pattern'])}"]
    if q.get("types"):
        args.append(f"--types {q['types']}")
    if salted:
        args += [f"--hit {q['hit']}", f"--hot-pool {q.get('hot_pool', 5000)}",
                 f"--unique-pattern {shlex.quote(q['unique_pattern'])}"]
    return f"python3 {LIB}/gen_queries.py " + " ".join(args)


def prepare_commands(exp, stack, topo, net, warmup, duration):
    """Shell commands per node that set up one experiment's fixtures, and its query service."""
    backend_ip = topo["nodes"]["backend"]["ip"]
    py = f"python3 {LIB}"
    cmds = {"dut": [], "loadgen": [], "backend": ["systemctl stop k8s-api nsd 2>/dev/null || true"]}
    queries = None
    for node in ("dut", "backend", "loadgen"):
        for svc in services(stack, node):
            n = svc["name"]
            if n == "hosts-data":
                cmds[node] += ["mkdir -p /etc/bench",
                               f"{py}/gen_data.py hosts --count {svc['count']} --zone {svc['zone']} --out /etc/bench/hosts"]
            elif n == "kubeconfig":
                cmds[node] += ["mkdir -p /etc/bench",
                               f"{py}/gen_data.py kubeconfig --server http://{backend_ip}:6443 --out /etc/bench/kubeconfig"]
            elif n == "nsd":
                z = svc["zone"]
                cmds[node] += [
                    f"{py}/gen_data.py nsd-zone --zone {z} --ns-ip {backend_ip} --out /etc/nsd/{z}.zone",
                    f"{py}/gen_data.py nsd-conf --zone {z} --zonefile {z}.zone --out /etc/nsd/nsd.conf",
                    f"tc qdisc replace dev eth0 root netem delay {net.get('backend_delay_ms', 1)}ms",
                    "systemctl restart nsd",
                ]
            elif n == "k8s-api":
                cmds[node].append("systemd-run --unit=k8s-api --collect /usr/local/bin/k8s-memory-api serve 0.0.0.0:6443")
            elif n == "queries":
                cmds[node] += [f"mkdir -p {DATA}/queries", queries_command(exp, svc, warmup, duration)]
                queries = svc
            elif n != "dns":
                raise SystemExit(f"unknown service {n}")
    if queries is None:
        raise SystemExit(f"stack of {exp['name']} has no queries service on loadgen")
    return cmds, queries


def ready_command(probe, ip):
    dig = f"dig @{ip} -p 53 +time=1 +tries=1 +noall +comments {probe['name']} {probe.get('type', 'A')}"
    cmd = f"{dig} | grep -q 'status: NOERROR'"
    if probe.get("answer"):
        cmd += f" && {dig} | grep -q 'ANSWER: [1-9]'"
    return cmd


def action_commands(actions, net):
    """(scheduled, cleanup): scheduled run at `at` seconds into the measured phase; cleanup after the run."""
    scheduled, cleanup = [], []
    for a in actions:
        if a["do"] == "reload":
            scheduled.append({"node": a["node"], "at": a["at"], "cmd": f"systemctl kill -s USR1 {UNIT}"})
        elif a["do"] == "delay":
            scheduled.append({"node": a["node"], "at": a["at"],
                              "cmd": f"tc qdisc replace dev eth0 root netem delay {a['ms']}ms"})
            cleanup.append({"node": a["node"],
                            "cmd": f"tc qdisc replace dev eth0 root netem delay {net.get('backend_delay_ms', 1)}ms"})
        else:
            raise SystemExit(f"unknown action {a['do']}")
    return scheduled, cleanup


def runs_for(exp, phase):
    out = []
    for proto in exp.get("protocols", {"peak": ["udp"], "sweep": ["udp"]}).get(phase, []):
        if phase == "peak":
            out.append({"label": f"{proto}-peak", "proto": proto, "mode": "peak", "frac": None})
        else:
            for frac in exp.get("sweep", [0.25, 0.5, 0.75, 0.9]):
                out.append({"label": f"{proto}-r{round(frac * 100)}", "proto": proto, "mode": "sweep", "frac": frac})
    return out


def build(root, scene, out_dir, topology=None):
    s = load(root, scene)
    topology = topology or s["00-nodes"].get("topology", "default")
    topo = yaml.safe_load(open(os.path.join(root, "topologies", topology + ".yml")))
    plan = {"scene": scene, "topology": topology, "experiments": {}, "blocks": []}
    os.makedirs(os.path.join(out_dir, "corefiles"), exist_ok=True)
    experiments = s["04-experiments"]["experiments"]
    names = [e["name"] for e in experiments]
    if len(set(names)) != len(names):
        raise SystemExit("duplicate experiment names")
    for exp in experiments:
        stack = s["stacks"][exp["stack"]]
        net = s["01-network"].get("network", {}).get(exp.get("network", "default"), {})
        warmup, duration = exp.get("warmup", 30), exp.get("duration", 60)
        cores_list = exp.get("dut_cores", [min(4, topo["nodes"]["dut"]["vcpus"])])
        for c in cores_list:
            if c > topo["nodes"]["dut"]["vcpus"]:
                raise SystemExit(f"{exp['name']}: dut_cores {c} exceeds dut vcpus")
        scheduled, cleanup = action_commands(s["03-actions"].get("actions", {}).get(exp.get("actions", "none"), []), net)
        prep, q = prepare_commands(exp, stack, topo, net, warmup, duration)
        plan["experiments"][exp["name"]] = {
            "prepare": prep, "ready": ready_command(q["probe"], topo["nodes"]["dut"]["ip"]),
            "warmup": warmup, "duration": duration, "queries": f"{DATA}/queries/{exp['name']}.txt",
            "salted": "hit" in q, "target_hit": q.get("hit"), "actions": scheduled, "cleanup": cleanup,
            "ref": exp.get("ref", exp["name"]), "runs_jsonl": f"{DATA}/results/{exp['name']}/runs.jsonl",
        }
        first = True
        for phase in ("peak", "sweep"):
            runs = runs_for(exp, phase)
            if not runs:
                continue
            for rep in range(1, exp.get("reps", 5) + 1):
                for cores in cores_list:
                    servers = exp["servers"]
                    for server in servers if rep % 2 else list(reversed(servers)):
                        srv = s["servers"][server]
                        cf = f"corefiles/{exp['name']}-{server}-c{cores}.Corefile"
                        text = render_corefile(stack["corefile"], srv, cores,
                                               f"{topo['nodes']['backend']['ip']}:53", "/etc/bench/kubeconfig")
                        with open(os.path.join(out_dir, cf), "w") as f:
                            f.write(text)
                        plan["blocks"].append({
                            "id": f"{exp['name']}-{phase}-rep{rep}-{server}-c{cores}", "exp": exp["name"],
                            "phase": phase, "rep": rep, "server": server, "cores": cores, "corefile": cf,
                            "prepare": first, "last": False, "binary": srv["binary"], "args": srv["args"],
                            "taskset": f"0-{cores - 1}", "runs": runs,
                        })
                        first = False
        for b in reversed(plan["blocks"]):
            if b["exp"] == exp["name"]:
                b["last"] = True
                break
    with open(os.path.join(out_dir, "plan.json"), "w") as f:
        json.dump(plan, f, indent=1)
    return plan


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--scene", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--topology", help="installed topology; overrides 00-nodes.yml")
    a = ap.parse_args()
    p = build(a.root, a.scene, a.out, a.topology)
    print(f"{len(p['blocks'])} blocks, {sum(len(b['runs']) for b in p['blocks'])} runs", file=sys.stderr)
