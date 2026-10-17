#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
preflight=$repo_root/scripts/agent-resource-admission
test_root=$(mktemp -d)
prefix=ss-codex-admission-e2e-$$
live_unit=$prefix-live.scope
self_unit=$prefix-self.scope
leftover_unit=$prefix-leftover.scope
live_runner=
self_runner=
leftover_runner=

cleanup() {
    systemctl --user stop "$live_unit" "$self_unit" "$leftover_unit" >/dev/null 2>&1 || :
    [ -z "$live_runner" ] || wait "$live_runner" 2>/dev/null || :
    [ -z "$self_runner" ] || wait "$self_runner" 2>/dev/null || :
    [ -z "$leftover_runner" ] || wait "$leftover_runner" 2>/dev/null || :
    rm -r "$test_root"
}
trap cleanup EXIT HUP INT TERM

cat >"$test_root/config" <<'EOF'
reserve_mib=0
swap_window_seconds=1
max_swap_growth_kib=1048576
psi_avg10_max=100
EOF
cat >"$test_root/capped-config" <<'EOF'
reserve_mib=0
swap_window_seconds=1
max_swap_growth_kib=1048576
psi_avg10_max=100
max_memory_jobs=1
EOF
cat >"$test_root/zero-cap-config" <<'EOF'
reserve_mib=0
swap_window_seconds=1
max_swap_growth_kib=1048576
psi_avg10_max=100
max_memory_jobs=0
EOF
cat >"$test_root/codex" <<'EOF'
#!/bin/sh
if [ "$1" = exec ] && [ "${2-}" = run-preflight ]; then
    shift 2
    "$@"
    exit
fi
if [ "$1" = exec ] && [ "${2-}" = heavy ]; then
    python3 -c 'import time; held = bytearray(512 * 1024 * 1024); time.sleep(120)'
    exit
fi
sleep 120
EOF
chmod +x "$test_root/codex"

run_preflight() {
    estimate=$1
    config=${2:-$test_root/config}
    "$preflight" "$estimate" --config "$config" --scope-pattern "$prefix-*.scope"
}

wait_for_memory() {
    unit=$1
    attempt=0
    while [ "$attempt" -lt 50 ]; do
        memory=$(systemctl --user show "$unit" -p MemoryCurrent --value 2>/dev/null || :)
        [ "${memory:-0}" -ge 503316480 ] 2>/dev/null && return
        attempt=$((attempt + 1))
        sleep 0.1
    done
    echo "E2E FAIL scope did not allocate expected memory: $unit" >&2
    exit 1
}

field() {
    name=$1
    for item in $output; do
        case $item in
            "$name"=*) printf '%s\n' "${item#*=}"; return ;;
        esac
    done
    return 1
}

wait_for_scope() {
    unit=$1
    attempt=0
    while [ "$attempt" -lt 50 ]; do
        [ "$(systemctl --user show "$unit" -p ActiveState --value 2>/dev/null || :)" = active ] && return
        attempt=$((attempt + 1))
        sleep 0.1
    done
    echo "E2E FAIL scope did not become active: $unit" >&2
    exit 1
}

expect_go() {
    label=$1
    estimate=$2
    config=${3:-$test_root/config}
    if ! output=$(run_preflight "$estimate" "$config"); then
        echo "E2E FAIL $label expected GO" >&2
        echo "$output" >&2
        exit 1
    fi
    echo "$output" | grep -q '^GO '
    printf '%s: %s\n' "$label" "$output"
}

expect_wait() {
    label=$1
    estimate=$2
    reason=$3
    config=${4:-$test_root/config}
    if output=$(run_preflight "$estimate" "$config"); then
        echo "E2E FAIL $label expected WAIT" >&2
        echo "$output" >&2
        exit 1
    fi
    echo "$output" | grep -q '^WAIT .*reason=.*'"$reason"
    printf '%s: %s\n' "$label" "$output"
}

expect_go nothing-running 1

systemd-run --user --scope --quiet --slice=agents.slice --unit="$live_unit" \
    "$test_root/codex" exec heavy &
live_runner=$!
wait_for_scope "$live_unit"
wait_for_memory "$live_unit"
expect_go one-live-job-no-cap 1
[ "$(field max_memory_jobs)" = off ]
[ "$(field active_memory_jobs)" = 1 ]
jobs_memory_mib=$(field active_jobs_memory_mib)
mem_available_mib=$(field mem_available_mib)
[ "$jobs_memory_mib" -ge 480 ]
! echo "$output" | grep -q 'effective_available_mib='
attempt=0
while [ "$attempt" -lt 10 ]; do
    required_mib=$((mem_available_mib - jobs_memory_mib / 2))
    cat >"$test_root/no-double-count-config" <<EOF
reserve_mib=$required_mib
swap_window_seconds=1
max_swap_growth_kib=1048576
psi_avg10_max=100
EOF
    if output=$(run_preflight 0 "$test_root/no-double-count-config"); then
        result=GO
    else
        result=WAIT
    fi
    jobs_memory_mib=$(field active_jobs_memory_mib)
    mem_available_mib=$(field mem_available_mib)
    if [ "$mem_available_mib" -ge "$required_mib" ] && \
        [ "$((mem_available_mib - jobs_memory_mib))" -lt "$required_mib" ]; then
        [ "$result" = GO ] || {
            echo "E2E FAIL heavy job memory was subtracted from MemAvailable" >&2
            echo "$output" >&2
            exit 1
        }
        printf '%s: %s\n' heavy-job-reported-not-double-counted "$output"
        break
    fi
    attempt=$((attempt + 1))
done
[ "$attempt" -lt 10 ] || {
    echo "E2E FAIL could not establish a stable double-count boundary" >&2
    exit 1
}
expect_go one-live-job-zero-cap 1 "$test_root/zero-cap-config"
expect_wait one-live-job-capped 1 active_jobs "$test_root/capped-config"
systemctl --user stop "$live_unit"
wait "$live_runner" || :
live_runner=

systemd-run --user --scope --quiet --slice=agents.slice --unit="$self_unit" \
    "$test_root/codex" exec run-preflight "$preflight" 1 --config "$test_root/capped-config" \
    --scope-pattern "$prefix-*.scope" >"$test_root/self-output" &
self_runner=$!
wait "$self_runner"
self_runner=
output=$(cat "$test_root/self-output")
echo "$output" | grep -q '^GO .*active_memory_jobs=0 '
echo "$output" | grep -q "self_scope=$self_unit"
printf '%s: %s\n' self-scope "$output"

systemd-run --user --scope --quiet --slice=agents.slice --unit="$leftover_unit" sleep 120 &
leftover_runner=$!
wait_for_scope "$leftover_unit"
output=$(run_preflight 1) || {
    echo "E2E FAIL leftover-scope expected GO" >&2
    echo "$output" >&2
    exit 1
}
echo "$output" | grep -q "^LEFTOVER scope=$leftover_unit age_seconds="
echo "$output" | grep -q '^GO '
printf '%s: %s\n' leftover-scope "$output"

expect_wait forced-pressure 999999999 headroom
echo "E2E PASS"
