# pharos-bench

Ansible + Python harness benchmarking Pharos vs CoreDNS on a KVM host (VMs `dut`, `loadgen`, `backend`). README.md has the full design; read it for scenario/metric semantics.

## Verify

- Only local check: `$(head -1 $(which ansible-playbook) | sed 's/#!//') -m unittest discover -s tests` (must use Ansible's python for PyYAML; plain `python3` may lack it). Covers `scripts/*.py`. No linter/CI.
- Everything else (`plays/install.yml`, `plays/scenario.yml -e scene=<name>`, `plays/process.yml`) needs a real remote KVM host in `hosts` (gitignored; copy `hosts.example`). Never run them without the user asking — they cost money/hours (AWS metal) and reboot VMs.
- `plan.py --scene <name> --out <dir> --topology default` can be run locally to inspect the expanded blocks/Corefiles without a host.

## Layout quirks

- Scenarios: `scenarios/<name>/{00..04}-*.yml`; missing files fall back to `scenarios/_defaults/`. Experiments' `uses: [other]` pulls stacks from another scenario (see `scenarios/smoke`). Corefile placeholders (`@MULTISOCKET@`, `@UPSTREAM@`, `@KUBECONFIG@`) are substituted in `scripts/plan.py`, same Corefile for both servers; `@SALT@` in query files is substituted per run by `scripts/bench-run.py`.
- `scripts/` are copied to the VMs; `plays/scenario.yml` re-pushes them (`roles/vm_guest/tasks/scripts.yml`) before each run, so script edits take effect without re-running `install`. Changes to anything else in `vm_guest`/`vm_host` need `install.yml`.
- `build/` and `results/` are gitignored generated output (plan/Corefiles, raw dnsperf/probe data); don't edit or commit. `scenarios/smoke-tcp`, `debug-nodelay`, `smoke-backends` are ad-hoc debugging scenarios (partly untracked).
- `scripts/process.py` produces `summary.csv/md`; sweep rates derive from the slower server's median peak (`scripts/refpeak.py`), so changing peak/ref logic affects sweep correctness.
- Pharos is cloned/built on the host from `pharos_repo`/`pharos_ref` in `group_vars/all.yml` (SSH agent key needed); CoreDNS/dnsperf versions are pinned there with sha256s — update both version and hash together.
- Infra: `terraform/aws` (use `tofu -chdir=terraform/aws`); `*.tfvars`/state are gitignored.
