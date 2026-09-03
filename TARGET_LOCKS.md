# Switchstand Contract target locks

This persistent coordination branch holds one lock file per Asana Contract-document task. There is no global or repository-wide implementation lock.

## Identity and record

The exact target Asana task GID is the lock ID. Its path on branch `coordination/implementation-lock` is:

```
target-locks/v1/<target-task-gid>.json
```

Each lock records `acquisition_id`, agent, owning task/change, environment, work, exact target task GID, and acquisition time.

## Acquire

One agent owns exactly one Contract change. That change may require locks for multiple target documents.

1. Determine the complete set of target Asana Contract-document task GIDs before mutation.
2. Sort the GIDs bytewise ascending.
3. In that order, create each lock path with GitHub Contents API create-file-if-absent.
4. Fetch each created path and require its `acquisition_id` to match before continuing.

Create-file-if-absent is the atomic same-target boundary. Different target-task paths do not block each other. If create returns a shared-branch 409, fetch that exact target lock: a present different acquisition is held and its complete holder/work record goes to Marco; a present matching acquisition confirms ownership; when the lock is absent and the response conclusively identifies branch-head movement, retry that create once; otherwise fail closed as ambiguous.

If any lock cannot be acquired and confirmed, perform no Contract implementation mutation. Release every lock acquired in this attempt using the release rule, verify each absent, and report the existing holder/work when present.

## Release and dead-holder recovery

Release only after final owning-system verification, including the owning change-task update. Fetch each lock, require the same `acquisition_id`, delete by that response's current blob SHA, then fetch again and require not found. Never delete on an ID mismatch or ambiguous readback.

Locks have no TTL and never expire, transfer, or clear automatically. Marco must authorize clearing one exact dead acquisition. Re-fetch it, require that approved `acquisition_id` unchanged, delete by current blob SHA, and verify absence. Then acquire normally; never overwrite or steal.
