#!/bin/bash
# bench-probe.sh start DIR SECONDS UNIT | stop DIR   (runs on the dut as root)
# bench-probe.sh cpu DIR SECONDS                      (any node: whole-VM CPU via mpstat, detached)
set -u
cmd=$1 dir=$2
scrape() { curl -sf -m 2 http://127.0.0.1:9153/metrics | grep -E '^(coredns|pharos)_cache_(hits|misses)_total' || true; }
case $cmd in
cpu)
  mkdir -p "$dir"; nohup mpstat 1 "$3" >"$dir/mpstat.txt" 2>&1 &
  ;;
sync)
  # state right after startup: before any run resets VmHWM, so the initial list/sync peak is kept
  unit=$3 ready=$4
  mkdir -p "$dir"
  pid=$(systemctl show -p MainPID --value "$unit")
  {
    echo "ready=$ready"
    [ "${pid:-0}" -gt 0 ] && grep -E '^Vm(HWM|RSS):' "/proc/$pid/status"
    echo "oom_kill=$(dmesg | grep -c 'Killed process')"  # dut reboots per block, so dmesg only holds this block
  } >"$dir/startup" 2>/dev/null || true
  ;;
start)
  secs=$3 unit=$4
  pid=$(systemctl show -p MainPID --value "$unit")
  [ "${pid:-0}" -gt 0 ] || { echo "no running unit $unit" >&2; exit 1; }
  mkdir -p "$dir"; echo "$pid" >"$dir/pid"
  echo 5 >"/proc/$pid/clear_refs"   # reset VmHWM so peak RSS covers this run only
  cp /proc/net/snmp "$dir/snmp.before"; cp /proc/stat "$dir/stat.before"; scrape >"$dir/metrics.before"
  nohup pidstat -u -r -p "$pid" 1 "$secs" >"$dir/pidstat.txt" 2>&1 &
  ;;
stop)
  pid=$(cat "$dir/pid")
  while pgrep -f "pidstat .* -p $pid " >/dev/null; do sleep 0.5; done
  cp /proc/net/snmp "$dir/snmp.after"; cp /proc/stat "$dir/stat.after"; scrape >"$dir/metrics.after"
  grep VmHWM "/proc/$pid/status" >"$dir/vmhwm"
  ;;
esac
