#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
preflight=$repo_root/scripts/agent-resource-admission
test_root=$(mktemp -d)
prefix=ss-codex-admission-e2e-$$
live_unit=$prefix-live.scope
leftover_unit=$prefix-leftover.scope
live_runner=
leftover_runner=

cleanup() {
    systemctl --user stop "$live_unit" "$leftover_unit" >/dev/null 2>&1 || :
    [ -z "$live_runner" ] || wait "$live_runner" 2>/dev/null || :
    [ -z "$leftover_runner" ] || wait "$leftover_runner" 2>/dev/null || :
    rm -r "$test_root"
}
trap cleanup EXIT HUP INT TERM

cat >"$test_root/config" <<'EOF'
reserve_mib=0
swap_window_seconds=1
max_swap_growth_kib=1048576
psi_avg10_max=100
max_memory_jobs=1
EOF
cat >"$test_root/codex" <<'EOF'
#!/bin/sh
sleep 120
EOF
chmod +x "$test_root/codex"

run_preflight() {
    "$preflight" "$1" --config "$test_root/config" --scope-pattern "$prefix-*.scope"
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
    if ! output=$(run_preflight "$estimate"); then
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
    if output=$(run_preflight "$estimate"); then
        echo "E2E FAIL $label expected WAIT" >&2
        echo "$output" >&2
        exit 1
    fi
    echo "$output" | grep -q '^WAIT .*reason=.*'"$reason"
    printf '%s: %s\n' "$label" "$output"
}

expect_go nothing-running 1

systemd-run --user --scope --quiet --slice=agents.slice --unit="$live_unit" \
    "$test_root/codex" exec &
live_runner=$!
wait_for_scope "$live_unit"
expect_wait one-live-job 1 active_jobs
systemctl --user stop "$live_unit"
wait "$live_runner" || :
live_runner=

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
