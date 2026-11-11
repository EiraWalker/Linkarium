# Share completed backup ZIPs (offline, two-root scope)

`share_completed_backups.py` preserves every original backup pathname and byte,
but replaces duplicate completed ZIPs with hardlinks to a preferred **active-root
inode**. These aliases are **not independent physical backups**. This is not a
ZIP search, general deduplicator, backup deletion tool, or data migration.

## Before running

1. Obtain explicit authorization for the two roots and loss of physical-copy
   independence. Keep the original checkpoint manifest unchanged.
2. The parent operator must hold the existing
   `<active-root-parent>/ops/v012-operator.lock` continuously from before the
   scoped stop through verification. Stop only the target Linkarium instance;
   establish actual process, mount and reader quiescence outside this tool.
   `--offline` is an explicit assertion, **not automatic service/PID discovery**.
3. Supply canonical absolute, nonoverlapping data-root paths on one filesystem.
   All root contents must be regular files/directories without symlinks or
   special files. Every hardlink alias of every inventoried file must be inside
   these two roots (or the exact operator lock / recognized staging paths).
   Unknown external aliases require review, not deletion or a relaxed check.
4. Pre-create a dedicated empty receipt directory outside both data roots,
   owned by the invoking user with mode `0700`. Use the same directory on every
   retry. The tool only adds `intent.json` and `complete.json`, both mode `0600`.
5. If `<cold-root-parent>/data-manifest.json` exists, independently review and
   supply its exact SHA-256 using `--legacy-manifest-sha256`. This version accepts
   the historical `files` entries with `kind=independent_copy` and full
   dev/inode/size/mode/uid/gid/mtime/SHA fields for the completed ZIPs. Other
   manifest schemas require separate review; they are not silently reinterpreted.

Linux with Python 3.11+ is required. Windows returns `LINUX_REQUIRED`; Windows
skips are not a directory-fsync/hardlink acceptance result. The program uses only
standard-library code and the adjacent `storage_upgrade.py`; it neither connects
to production nor invokes Docker, services, Nginx or authentication code.

## Invocation

Use parent-reviewed values, not guessed checkpoint paths. This example assumes
`OPERATOR_FD` is an already-held, inherited exclusive `flock` descriptor for
`OPERATOR_LOCK`. An unlocked, shared-locked, or differently bound descriptor is
refused before any intent or backup write. A separate open of the same inode
cannot stand in for the inherited locked description. The tool neither upgrades
a shared lock nor unlocks or closes that caller-owned description. The parent
must keep that description exclusively locked throughout the invocation. This
validation relies on native local Linux `flock` semantics; no NFS/SMB behavior
is newly supported by this contract.

```sh
python3 -B -S deploy/cloud/share_completed_backups.py \
  --active-root "$ACTIVE_DATA" \
  --cold-root "$COLD_DATA" \
  --operator-lock "$OPERATOR_LOCK" \
  --operator-lock-fd "$OPERATOR_FD" \
  --receipt-dir "$SHARING_RECEIPT" \
  --legacy-manifest-sha256 "$REVIEWED_OLD_MANIFEST_SHA256" \
  --offline
```

This is a validating **dry run**: all supervisor locks are acquired, receipts and
full archive bytes are checked, but no backup, stage or receipt is written.
Repeat the same invocation with `--apply` while the parent's stop/lock scope
remains in effect. Omit the manifest option only when no historical manifest
exists. A standalone invocation without `--operator-lock-fd` acquires the same
operator lock itself, but cannot establish a continuous lock spanning an earlier
service stop; production coordination remains the parent's responsibility.

`share(active, cold, *, operator_lock, offline, apply=False,
operator_lock_fd=None, receipt_dir=None, legacy_manifest_sha256=None)` exposes the
same guarded operation to a parent Python operator. `receipt_dir` is mandatory
for apply. Locks are retained until validation, publication, receipt syncing and
final measurements finish. Supervisor lock aliases are acquired once per inode.

The CLI emits one JSON object and an exit code: `0` for the completed invocation,
`1` for refusal/failure. Argument errors, including `--help`, use privacy-safe
JSON rather than echoing arguments. Values contain only booleans, integer counts,
digests and reason codes—no backup names, URLs, titles, rows or OS exception text.
The Python API retains the original exception for its caller to handle privately.

## Eligibility and publication contract

- Reuse `storage_upgrade.completed_backups` and its succeeded backup/restore
  receipts. A succeeded restore's `pre_restore_backup` may have no recorded size
  or SHA: its actual full bytes are still hashed. Unfinished/unreceipted ZIPs
  remain unchanged. Missing receipted paths fail closed in this first version.
- Check canonical raw receipt spellings before the helper normalizes paths.
  Reject nonempty SQLite WAL/journal/SHM files before immutable reads. Neither
  checkpointing nor recovery of SQLite is attempted.
- Collect metadata before archive reads. Same-named candidates must have matching
  mode, uid, gid and nanosecond mtime, even if a later hash would separate them.
  Content groups are checked again. Any extended attribute is refused (including
  ACL metadata); no `chmod`, `chown`, timestamp or xattr repair is performed.
  Reads may change atime; link operations necessarily change ctime and nlink.
- Hash each archive in full and validate any completion-receipt size/SHA. Group
  by device, byte size and actual SHA; active-root paths are considered first.
  State-DB contents are read only for completion evidence; only a whole-file
  digest enters the private conversion receipt, never database rows.
- Bind input files, ancestors, locks, evidence directory and all aliases. Recheck
  identity/metadata/link counts before publication and after readback. Pin source
  and destination directory descriptors for link/replace. These checks require
  the caller's real offline freeze; advisory locks do not protect against a
  hostile privileged process continuously replacing directories.
- Fsync the retained source inode. Create a same-directory hardlink named
  `.<target-basename>.completed-share-v1`, validate its source-inode binding,
  atomically replace the target with `os.replace`, fsync the directory, then
  read/hash back. No original source pathname is deleted.
- A recognized leftover stage can be reused only when it is already the bound
  preferred source inode. If the target is already that inode, only this exact
  bound stage can be unlinked. Unknown/replaced stages remain untouched and cause
  refusal. There is no recursive cleanup or cache/history removal.

## Historical manifest and retry evidence

`intent.json` is durable **before** any backup replacement. It records root-path
and relative-name digests, root/database identities, actual content SHAs, the old
manifest SHA, and each original/planned dev/inode/nlink/size/allocation/mode/
uid/gid/mtime tuple. Names are hashed, not printed. `complete.json` binds that
exact intent digest and the resulting metadata. Existing evidence is validated
and re-fsynced, never overwritten or removed.

The old manifest remains byte-for-byte unchanged. After sharing, its original
independent-inode/physical-copy assertion is historical, not a valid current
physical acceptance check. Use the original manifest **together with the new
conversion evidence** for these ZIPs; this tool does not rewrite or waive any
other checkpoint, rollback or later artifact-migration invariant.

`legacy_physical_independence_superseded` reflects the actual current cold
inodes, including a dry run after an earlier successful apply. When no legacy
manifest exists, `legacy_manifest_sha256` uses SHA-256 of empty bytes as the
absent-manifest marker and the superseded flag is false.

On interruption, keep every file and the original receipt directory. Re-run the
same command under the same offline/lock conditions. Each path must still match
its recorded original or planned inode, with the same bytes and metadata. A
fresh same-byte but unbound inode is refused. Already-shared paths are rehashed
and directory-fsynced before success, including after a failed prior fsync.
Full matching evidence can be re-fsynced; partial, conflicting or modified
receipts require manual review and are not erased to make a retry pass.

A failure after some replacements is **not a rollback**: earlier aliases may
already be shared, while all original backup paths remain readable. A persisted
`complete.json` alone does not establish that a later invocation, final free-space
query, output delivery or lock cleanup succeeded; the actual invocation outcome
and current readback remain authoritative.

## Space accounting and acceptance

The 512 MiB reserve is retained. A fresh filesystem free-space check (plus 64 KiB
publication allowance) runs before intent publication and before each group.
Final free space is measured again after receipt publication; a reserve failure
is not reported as success.

- `logical_bytes`: sum of bytes at every completed-backup path.
- `unique_allocated_before_bytes` / `unique_allocated_after_bytes`: distinct
  completed-backup inode allocation (`st_blocks * 512`), not summed hardlinks and
  not whole-root application allocation.
- `tree_allocation_reduction_bytes`: the difference for that completed-backup
  inventory during **this invocation**; retries may show only remaining progress.
- `filesystem_free_before_bytes`, `filesystem_free_after_bytes`, and
  `filesystem_free_delta_bytes`: independent fresh observations of the shared
  root filesystem. Receipt allocation and unrelated filesystem activity affect
  these values. Open readers can keep an unlinked old inode allocated, so the
  namespace reduction is **not a promise of immediately usable free space**.

Before the subsequent artifacts migration, the parent must read fresh actual
free space and repeat its full cumulative rollback/reserve capacity gate. This
sharing receipt does not authorize lowering that gate or starting other services.

## Local verification

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests/deploy \
  python -S -m unittest -v test_share_completed_backups \
  test_storage_upgrade test_storage_rollback
```

Run POSIX fixtures on native Linux storage. The task harness uses a local pinned
image, no pull, `--network=none`, no Docker socket, read-only sources, and private
native tmpfs fixtures bounded by 2 GiB. Tests use real SQLite succeeded receipts,
real ZIPs and hardlinks, child-process locks, real process exits after link and
replace, metadata/alias drift, injected fsync failures and held original readers.
The local WSL tmpfs does not support setting user xattrs; its nonempty-xattr read
result is explicitly fault-injected. This is local filesystem/control-flow
acceptance, not a production execution, reboot/power-loss proof, or independent
review approval.
