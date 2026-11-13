# pharos-bench

Reproducible DNS benchmarks of [Pharos](https://github.com/itsmeyaw/pharos-dns) against CoreDNS v1.15.0 on a single
Linux/KVM host. The structure follows the [EnGINE framework](https://github.com/rezabfil-sec/engine-framework):
Ansible plays for `install → scenario → process`, scenarios as five YAML files, services as roles, node names as pseudonyms.

```
laptop (ansible) ──ssh──▶ host (bare metal, KVM) ─┬─ VM dut      runs ONE server at a time (pharos | coredns)
                                                  ├─ VM loadgen  dnsperf 2.16
                                                  └─ VM backend  NSD upstream (+1 ms netem) or synthetic Kubernetes API
```

The VMs talk over a private bridge (`10.77.0.0/24`); benchmark traffic never touches the host's physical NIC.

## Quick start

```sh
# 1. A host. Either bring your own (see "Host contract") or create the AWS one:
tofu -chdir=terraform/aws init
tofu -chdir=terraform/aws apply          # c7i.metal-24xl, us-east-1, self-terminates after 8 h
tofu -chdir=terraform/aws output -raw inventory > hosts
#    own host: cp hosts.example hosts and edit

# 2. Build binaries, create and configure the VMs (~20-30 min the first time)
ansible-playbook plays/install.yml

# 3. Run a scenario, then summarise it
ansible-playbook plays/scenario.yml -e scene=smoke
ansible-playbook plays/process.yml              # newest run; or -e run_id=<dir in results/>

# 4. Done
ansible-playbook plays/cleanup.yml              # optional, removes VMs from the host
tofu -chdir=terraform/aws destroy
```

Needs on your laptop: Ansible ≥ 2.14, an SSH key at `~/.ssh/id_ed25519` (change `bench_ssh_key` in
`group_vars/all.yml`) loaded in your SSH agent (the host clones Pharos with it unless the repo is public),
and for AWS OpenTofu ≥ 1.6 (or Terraform ≥ 1.5) plus credentials. `process` uses matplotlib for PNGs if it is installed.

`plays/all.yml -e scene=<name>` runs install, scenario and process in one go.

## Host contract

Ansible only needs SSH. `plays/install.yml` fails fast unless the host has:

- x86_64 Ubuntu 22.04/24.04 or Debian 12, passwordless sudo, outbound internet (apt, GitHub, Ubuntu cloud images);
- `/dev/kvm`;
- enough **physical cores** (one thread per core is used, siblings stay idle) and memory for `topologies/default.yml`
  (4 host + 8 dut + 16 loadgen + 8 backend cores, 80 GiB). Smaller hosts: use or write another file in `topologies/`
  and pass `-e topology=<name>` to `install.yml` and `scenario.yml`.

The host is treated as dedicated and disposable: `install` installs QEMU/libvirt, a libvirt network `pbench`, VM disks under
`/var/lib/libvirt/images/pharos-bench`, and confines the host's own services and SSH sessions to the first 4 cores with
systemd `AllowedCPUs` (no `isolcpus`, so no reboot). `plays/cleanup.yml` removes all of it except packages.
A virtualized host with nested virtualization works but the manifest says `nested: true`, the summary carries a warning,
and the numbers are not comparable to bare metal.

## Scenarios

`scenarios/<name>/` holds the EnGINE five files; missing files fall back to `scenarios/_defaults/`.

| file | contents |
|---|---|
| `00-nodes.yml` | topology name (overridden by `-e topology=`) |
| `01-network.yml` | named network settings (backend reply delay) |
| `02-stacks.yml` | Corefile and per-node services: `hosts-data`, `kubeconfig`, `nsd`, `k8s-api`, `queries` |
| `03-actions.yml` | disturbances: Corefile reload (SIGUSR1) and a backend delay step to 50 ms, at N s into the measured phase |
| `04-experiments.yml` | servers, `dut_cores`, protocols per mode, sweep fractions, reps, warmup, duration, `actions`, `ref` |

Both servers get the **same Corefile**; `@MULTISOCKET@` becomes `multisocket <dut_cores>`.

| scenario | what it exercises |
|---|---|
| `smoke` | `whoami`, 1 repeat, 5 s warmup + 10 s measured, peak and one sweep point (checks the whole pipeline; ~10 min) |
| `whoami` | floor and stack used by `smoke`; also CoreDNS without `multisocket` (informational) |
| `cache` | `cache` + `forward` at designed hit ratios 0 / 50 / 95%; 95% runs on one core |
| `tail` | latency under steady load, bursts and cache churn |
| `memory` | Kubernetes scale and memory limits |
| `ndots` | negative caching and forwarding under search amplification |

Run one at a time (`-e scene=cache`). Write your own by copying a directory.

## Performance focus

The benchmark is ranked by what matters for a cluster DNS server, not by raw throughput. Rate-limited runs
(`sweep`) are fractions of the slower server's peak, so both servers see the same absolute load.

| # | focus | scenario | status |
|---|---|---|---|
| 1 | Tail latency (p99, p99.9) under burst load and churn | `tail` | done |
| 2 | Memory footprint and OOM resilience at high cluster scale | `memory` | done |
| 3 | Negative caching and upstream forwarding under `ndots` amplification | `ndots` | done |
| 4 | Watch-to-serve propagation latency during pod rollouts | | not built (the synthetic API never emits watch events) |
| 5 | Binary size and cold-start time | | not built |
| 6 | Peak single-core throughput (raw cached QPS) | `cache` (`cache-hit95`) | done, lowest priority |

- **`tail`**: `tail-steady` (50% of peak) is the baseline. `tail-burst` adds a 1 s burst of +40% of peak every 10 s
  (extra `dnsperf` processes; one merged latency histogram, so p99/p99.9 include the burst queries). `tail-churn` adds an
  undersized cache (constant eviction) and a Corefile reload at 30 s.
- **`memory`**: 10k / 50k / 100k / 250k Services and EndpointSlices from `scripts/k8s_api.py` (backend service
  `k8s-api` with `count:`). `summary.md` has a *Startup* table with peak RSS right after the initial sync (the number
  that decides OOMs) next to the usual RSS under closed-loop load. `mem-*-512m` / `mem-*-1g` run the server under a cgroup
  `MemoryMax` (no swap); an OOM kill during sync shows as `ready < blocks` and a non-zero OOM count. Adjust `mem_max` and
  the scales to your hardware: the limits shipped are guesses.
- **`ndots`**: each logical lookup becomes 10 queries (A+AAAA for 4 search suffixes that are NXDOMAIN upstream, then the
  real name), 80% negative. `ndots-nocache` isolates forwarding, `ndots-hit0` is the all-cold worst case (every
  negative answer is inserted, none reused), `ndots-hit95` is the warm negative cache. `lookups/s` is QPS divided by 10;
  `denial hit %` is negative-cache hits as a share of all lookups.

## What a run does

For each experiment, `scripts/plan.py` expands the scenario into *blocks* (one server at one core count), alternating
server order between repeats (A,B,B,A,…); all peak blocks come first, then sweep blocks. Each block:

1. reboots the `dut` VM (cold start), installs the Corefile, starts the server pinned with `taskset` under `systemd-run`;
2. waits until the stack's probe query is answered;
3. for each run: warmup `dnsperf` (discarded), start probes on the dut, measured `dnsperf`, stop probes.

Modes: **peak** is closed loop (64 clients, 8 threads, 512 in flight); **sweep** is open loop at fixed rates
`-Q` = fraction (25/50/75/90%) of the **slower server's median peak** from the same experiment (`ref:` can point at
another experiment), so both servers see identical absolute load. Defaults: 30 s warmup, 60 s measured, 5 repeats,
UDP for peak and sweep, TCP for peak on `whoami`.

Workload (`scripts/gen_queries.py`, seeded, so identical for both servers): Zipf(1.0) name popularity, type mix
A 65 / AAAA 25 / TXT 4 / MX 3 / SRV 3 (A/AAAA for Kubernetes), with workload-specific NXDOMAIN rates,
EDNS0 on every query (`dnsperf -e`). For `cache`, the hit ratio is built into the stream: a fraction of queries come
from a small hot pool, the rest are never-repeated names (`@SALT@` is replaced per run so warmup and measured
phases do not reuse names).

## Results

`results/<run-id>/` contains `manifest.json` (Pharos/CoreDNS versions and SHA, host CPU, kernel, nested flag, topology),
`plan/` (the exact Corefiles and plan), raw `dnsperf` output and probes per run, and after `process`:
`summary.csv`, `summary.md` and PNGs (p99 vs. QPS per experiment), plus the per-repetition data:
`runs.csv` (one row per measured run: `rep`, qps, p50/p90/p99/p99.9/max, loss, CPU, RSS, hit ratios, `valid`/`why`; use it for box plots)
and `latency_hist.csv` (one row per latency-histogram bucket per run: `upper_s`, `count`, `cum_frac`; use it for CDFs, grouping by
`exp, server, cores, proto, label, rep`). Invalid runs are kept and flagged, so filter on `valid`. Burst runs hold the steady run and all
bursts in one histogram.

Per run: completed QPS, loss, p50/p90/p99/p99.9 (nearest-rank over `dnsperf`'s latency-histogram bucket upper bounds,
about 3% resolution, loss excluded), the worst latency seen, the server's CPU % (`pidstat`, 100 = one core), peak RSS
(`VmHWM`, reset at run start), cache hit/miss counters (and the negative-cache share). Per block (`startup.csv`):
whether the server became ready, `VmHWM`/`VmRSS` right after the initial sync, and OOM kills from `dmesg`.
`/proc/net/snmp` (UDP receive-buffer errors) and `/proc/stat` before/after are kept raw in the
probe directory but not yet summarised. `summary` reports medians with bootstrap 95% CIs. A run is **invalid** (counted, excluded) if a sub-peak run loses more than 1% of its queries
(burst runs are exempt) or the achieved cache hit ratio is more than 5 points from the design target (`check_hit: false` turns that off).

## Cost

AWS `c7i.metal-24xl` on demand is roughly $4–5/h. Runtime depends on the scenarios and repetitions selected; run
scenarios individually or adjust `-var ttl_hours` if needed. The TTL guard lives in Terraform only (`shutdown -h` + terminate on shutdown); with another provider
you have to bring your own. `terraform destroy` as soon as you are done.

## Debugging the harness cheaply

`topologies/small.yml` fits a `c7i.4xlarge` with nested virtualization (about $0.7/h):

```sh
tofu -chdir=terraform/aws apply -var instance_type=c7i.4xlarge -var nested_virtualization=true
ansible-playbook plays/install.yml -e topology=small
ansible-playbook plays/scenario.yml -e scene=smoke -e topology=small
```

## Known limits

- One server at a time on one host; VMs only (node names are pseudonyms, so external hosts could be mapped later).
- Pharos has no `file` plugin, so authoritative serving is `hosts`; `sign` is not benchmarked.
- All queries carry EDNS0 (dnsperf has no per-query switch); no DoT/DoH; x86_64 only.
- Guest-side CPU isolation relies on `vcpupin`/`taskset`/cgroup cpusets, which is weaker than `isolcpus`.
- Cache hit-ratio design assumes `max_qps` (250k) bounds the offered rate; raise it per experiment if you see more.
- Vcpu steal is not measured from the host; the guest's `/proc/stat` is kept.
- `memory`: the synthetic API never emits watch events (no churn in the cache after the sync), and an OOM kill *after* startup
  aborts the run instead of being recorded. `tail` churn is cache eviction plus a Corefile reload, not Kubernetes object churn.
- p99.9 rests on the slowest 0.1% of a run's queries (a few thousand at typical rates), and bursts add only a few seconds of
  burst traffic per run; read the CI, not the point value.

## Tests

```sh
$(head -1 $(which ansible-playbook) | sed 's/#!//') -m unittest discover -s tests   # uses Ansible's python (PyYAML)
```
