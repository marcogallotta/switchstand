# Agent resource admission

Stage 1 is a procedural safety check for a small machine, not a scheduler. A dispatch-capable
Claude or Codex head runs it immediately before launching or resuming an agent, adding
memory-bearing fan-out, or starting a memory-heavy local operation. A worker gives its head a
conservative MiB estimate. One `GO` covers only that action; delay or changed work requires a
new check.

It does not kill applications, change cgroup limits, reserve memory atomically, or repair the
old resource broker. It can be bypassed and two uncoordinated heads can race. Stage 2 owns
enforcement research.

## Trial settings

The live trial reads `/home/marco/.config/switchstand/agent-resource-admission.conf`. Its initial
contents are copied from `config/agent-resource-admission.conf.example`:

```text
reserve_mib=3072
swap_window_seconds=5
max_swap_growth_kib=32
psi_avg10_max=2
# Optional hard cap; absent or 0 disables the count gate.
# max_memory_jobs=0
```

These are conservative starting points, not permanent limits. Change this file, not source code,
when a measured trial justifies one different value. There is deliberately no configuration
framework or validator. If the file is missing or unreadable, return `WAIT`.

The Coordinator owns evidence-based trial tuning under Marco's standing direction: change one
setting at a time, record the decision and outcome below, read back the file, and restore the
last known-good value on a warning. This does not require a fresh approval for each measured
adjustment. `max_memory_jobs` counts only live background `ss-codex-*.scope` jobs detected by
this script; it is not the native-agent slot count and must not be used as an idle-capacity or
ready-work signal. Native slots remain separately reported by the host. Do not add automatic
idle dispatch until a trustworthy ready-work signal is available.

The effective `agents.slice` values reported by systemd are the containment truth. They are not
duplicated here, and changing them is a separately authorized activation action.

## GO or WAIT

Immediately before the action, run `scripts/agent-resource-admission <estimate_mib>`. It:

1. Read `MemAvailable` from `/proc/meminfo` and `some avg10` from
   `/proc/pressure/memory`.
2. Read `pswpin` and `pswpout` from `/proc/vmstat`, wait `swap_window_seconds`, and read them
   again. Calculate `swap_growth_kib = ((delta_pswpin + delta_pswpout) *
   host_page_size_bytes) / 1024`. A missing counter or either negative delta/reset means `WAIT`.
3. Re-read `MemAvailable` and PSI after the swap window. Read the named `TasksCurrent`,
   `MemoryCurrent`, and `MemoryMax` properties for `agents.slice`; do not infer property order.
4. Count active memory-bearing jobs from live `ss-codex-*.scope` units in `agents.slice` that
   contain a live `codex exec` process. Report a live scope without that process as `LEFTOVER`
   with its name and age, but do not count it. When called from inside one of those active job
   scopes, exclude that caller scope from the count and report it as `self_scope`. Report the
   summed `memory.current` for all active job scopes (including the caller) as information only;
   `MemAvailable` already accounts for that memory.

The script prints the measurements and reason. `GO` exits zero; `WAIT` and measurement failure
exit nonzero.

Return `GO` only when all are true:

- `MemAvailable >= reserve_mib + estimate_mib`;
- `swap_growth_kib <= max_swap_growth_kib`;
- memory PSI `some avg10 <= psi_avg10_max`;
- when the optional `max_memory_jobs` is positive, active memory-bearing jobs other than the
  caller are fewer than it; and
- boxed work's estimate fits the effective slice capacity.

After `WAIT`, pause for at least `swap_window_seconds` and take one fresh full reading. If that
also returns `WAIT`, tell the parent the measured reason and do not keep retrying automatically.

At zero slice tasks, retained file cache in `MemoryCurrent` does not by itself cause `WAIT` when
the estimate fits `MemoryMax`. With active work, wait for it to complete and recheck; do not
pretend cached memory is a precise live-job measurement.

If host headroom is the only failed condition, calculate
`D = reserve_mib + estimate_mib - MemAvailable`. Ask Marco to close Firefox only when `D > 0`
and the summed `MemoryCurrent` of active Firefox cgroups is at least `D`. Say closing it *should*
make the action fit, never that it will, and run the whole check again after closure.

## Trial record

The activation operator appends JSON lines to
`/home/marco/.local/state/switchstand/agent-resource-admission-trial.jsonl`. Before each result,
append one `decision` line of at most 2 KiB. After completed `GO` work, append a linked `outcome`
line. Use one append per line and one serialized dispatch head as the writer; ambiguous ownership
means `WAIT`.

A decision records only its id/time, config hash, short work and action labels, estimate, the
host/PSI/swap-window/slice measurements, optional Firefox usage and deficit, and verdict/reason.
An outcome adds end or peak observations, `ok|degraded|unusable|not-observed` usability,
intervention, and result. Never record prompts, transcripts, credentials, or unrelated process
details. This file is trial evidence, not a permanent telemetry system.

## Conservative trial and rollback

Claude is the later live activation/trial operator under separate authority. Freeze the initial
file and hash, then run ordinary work one job at a time, smaller first. Do not manufacture an OOM.
The baseline is unqualified until representative light, implementation-like, and heavy work have
all completed without a warning and Marco reports that the desktop and ChatGPT remained usable.

A failed pre-admission reading is simply `WAIT`: let pressure settle and take a fresh full
reading without changing the file. Warning handling begins only after an admitted `GO`. A warning
is then swap or PSI above the configured value, desktop/ChatGPT degradation, a containment action,
oomd/OOM, or unavailable fresh evidence. Stop new dispatch and preserve the log. If the initial
baseline is still unqualified, propose a more conservative file where one exists. After
qualification, snapshot the exact current file before changing exactly one key; on a warning
restore that snapshot and read back both the file and effective systemd limits. A clean single
observation supports the next trial step, not a permanent figure.

## Activation edits (candidate only)

Replace the stale `RESOURCES` line in `codex-runs/rules-header.txt` with:

```text
- RESOURCES [M]: before launch, resume, added memory fan-out, or a heavy local step, the dispatch head follows docs/agent-resource-admission.md. One GO covers one action; WAIT is reported with its measured reason. Never assume a 5 GiB box, change a limit, or ask Marco to close Firefox without the measured deficit rule.
```

In `codex-runs/JOBS.md`, replace the fixed “5G / max 3” policy with the same preflight rule. Make
both initial launch and resume use the existing containment prefix:

```text
nohup setsid env CODEX_HOME=/home/marco/.local/state/switchstand/codex-bg-home systemd-run --user --scope --quiet --slice=agents.slice --unit=ss-codex-<heavy|light>-<name>-$(date +%H%M%S) codex exec ...
```

Choose `heavy` for implementation and other memory-bearing work, including the Codex mailbox
Coordinator. Choose `light` only for bounded review work. The sync checklist counts these existing
unit identities and flags more than four heavy or six total live jobs.

During activation, every dispatch-capable head must acknowledge the rule on its next interaction
or re-enter with the updated prompt. Until then, mark it unable to dispatch. Do not edit
`~/.claude`.
