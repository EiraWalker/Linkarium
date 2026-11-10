# Storage-compatible code upgrade operator

`storage_upgrade.py` is a **one-host, code-only upgrade**, not a data migration or disaster-recovery tool. It does not change Nginx, initialize authentication, execute migrations, compress artifacts, restore a database, pull an image, or install dependencies.

## Scope and prerequisites

- Run with Linux root and Python 3.11+ on the source host. `BASE` is fixed at `/srv/linkarium`; there is no base-path override. Docker is explicitly addressed through the local Unix socket, not inherited Docker contexts.
- Existing layout: `current` is a symlink to a direct child of `releases/`; `production.env` is root-owned `0600` with a single hardlink; `data/`, `releases/`, and `ops/` already exist. Base/config parents must be root-owned and not group/world-writable.
- This bounded operator expects the existing single-service `linkarium` Compose project, `linkarium-app-1`, the canonical cloud Compose file, one bind mount from `/srv/linkarium/data` to `/data`, and one `127.0.0.1` HTTP publication. Capture overlays or other deployment variants fail closed; do not silently remove their mounts/configuration to satisfy this operator.
- Use a committed package produced by `scripts/build-deploy.py`. Obtain its SHA-256 and complete commit through the approved release/build workflow. A package's own metadata is not an independent signature. Keep uploaded packages and operator code under root control and do not edit them while a phase runs.
- The existing image must have matching version/revision labels and matching application file hashes. Requirements, canonical Dockerfile, Compose file, and migration-file sets/hashes must match old vs new release. Missing/extra image source files also fail the source proof.
- Actual container `Config.Env`, `NetworkSettings.Ports` and `HostConfig.PortBindings` must match resolved Compose plus the **actual pinned image's** environment defaults. All configured environment keys are compared, not merely an auth-key allowlist or the disk env hash. Missing/duplicate/extra actual environment entries or publications fail closed; legitimate image defaults and Compose overrides are included in the expected result. Only present `LINKARIUM_VERSION` / `LINKARIUM_REVISION` fields may change to their exact expected release values; their presence is also bound.
- Image preparation deliberately uses the local legacy builder (`DOCKER_BUILDKIT=0`), an exact verified `sha256:` image ID, `--pull=false`, and `--network=none`. An unsupported legacy builder is a blocker, not permission to fetch a Dockerfile frontend, use BuildKit remotely, or install packages. The base is switched temporarily to root only to delete `/app/backend` and `/app/frontend`; replacement code is then copied and the original `10001:10001` user restored.
- Use an idle maintenance window. Close clients/extensions that can mutate authentication/device/session state during verification. There is no temporary Nginx write fence. Concurrent legitimate authentication activity can fail the strict continuity comparison; this never permits restoring an older authentication DB.
- Normal supervisor cleanup of an expired session can also fail strict identity equality, even without clients. This remains a conservative refusal requiring review, not an exception, permission to restore credentials, or a tolerance added by this repair.

## CLI

Supply the approved target version/commit/package values, not working-tree values. Reconfirm the live base immediately before preparation. The previously observed base was `32b1b857b1ba3dc7b5835a5e750cef2ef4ed8576`; do not treat that historical observation as a fresh inspection.

```sh
# OP points to the reviewed operator installed outside the release being built.
: "${OP:?}" "${VERSION:?}" "${COMMIT:?}" "${EXPECTED_BASE:?}"
: "${PACKAGE:?}" "${PACKAGE_SHA256:?}"

python3 "$OP" prepare \
  --version "$VERSION" --commit "$COMMIT" --expected-base "$EXPECTED_BASE" \
  --package "$PACKAGE" --package-sha256 "$PACKAGE_SHA256"

python3 "$OP" activate \
  --version "$VERSION" --commit "$COMMIT" --expected-base "$EXPECTED_BASE"

python3 "$OP" verify \
  --version "$VERSION" --commit "$COMMIT" --expected-base "$EXPECTED_BASE"
```

`--commit` and `--expected-base` are full lowercase 40-character hashes; package SHA-256 is 64 lowercase hex characters. Version follows the build script's `vX.Y.Z[-suffix]` convention. Package arguments are accepted only for `prepare`.

Journal: `/srv/linkarium/ops/storage-upgrade-${VERSION}-${COMMIT}/`, root `0700`. JSON receipts and saved `production.env` are `0600`. The old release stays in place, and the journal records its manifest hash, source proof and exact image ID. Images are **not** exported to large tar files. Do not prune old releases/images during this window. The operator shares `ops/v012-operator.lock` with the reviewed older operator.

### Prepare

Validates current release metadata/manifest, container scope, environment metadata, outer package SHA, every internal `SHA256SUMS` entry and exact file set. Extraction permits only plain directories/files under the expected release root: no traversal, ambiguous paths, symlinks, hardlinks, devices or duplicate archive members. Expansion is bounded. Package SHA and old/new manifests are checked again to reject drift. Offline image content is checked in a read-only, network-disabled, unprivileged container with **no production data mount**.

Preparation compares runtime configuration before building and rechecks it afterward. It resolves prospective target Compose using only the validated new version/revision in the child environment and the new pinned image defaults, without rewriting the live env. The canonical configuration digest must remain equal across old/target configurations. Resolved environment values stay in memory; only the digest is saved in receipts, never the values. The existing private, byte-exact `production.env` checkpoint used for config rollback is unchanged by this rule.

Preparation never stops the service. A prepared journal/release is not silently overwritten on retry. A failed/partial preparation requires inspection before a new attempt; it is not automatically removed. Journals created before these configuration/count gates lack required evidence and must not be reused to bypass preparation.

### Activate

1. Recheck prepared release/image/environment bindings and idle state. Compare the actual old runtime configuration and prospective target configuration to the prepared digest; repeat these checks after the space scan immediately before entering the stop boundary. A pre-stop drift refusal does not stop/recreate the service or enter rollback. Compute actual bytes requiring independent copying, conservative filesystem/journal overhead, free space, and a fixed **512 MiB reserve before stopping**. Repeat the budget check after stopping. Free-space checks are not a global reservation against unrelated disk writers.
2. Stop only project `linkarium`, service `app`; acquire the existing supervisor lock. Save `counts-before.json` **after stopping, while holding that lock**, covering the active generation, bookmark/snapshot/artifact counts, maintenance mode and active maintenance/job counts. No schema/auth initializer runs.
3. Create and verify a cold checkpoint:
   - SQLite databases, sidecars, keys, work files and other mutable files are independent copies with per-file size/SHA-256 verification.
   - Canonical `generations/<generation>/artifacts/<prefix>/<sha>.html[.gz]`, with a matching shard prefix, use same-device hardlinks and inode/size/mtime checks. Shared inodes are never chmod/chowned.
   - Direct `data/backups/*.bookmarklib.zip` may be linked **only** with a matching succeeded backup receipt, or a succeeded restore's named pre-restore archive. Backup receipts bind size/SHA where available; linked ZIP bytes are SHA-verified. Unfinished/unproven ZIPs are ordinary copies and may therefore cause the **pre-stop** space gate to reject the deployment. Do not add a blanket ZIP exemption.
   - Existing hardlink aliases are allowed. Creating another alias of a multi-GiB completed ZIP does not allocate another full ZIP.
4. Reconcile both the cold copy and still-locked live data against `counts-before.json`; every field must match. Compare cold and live authentication summaries. Covered tables: `auth_user`, `runtime_authstate`, `runtime_passkeycredential`, `runtime_filekeycredential`, `runtime_providersecret`, `runtime_executiondevice`, `django_session`; identity key files are also bound. Only hashes/counts are recorded, never row contents.
5. Preserve all environment bytes except the two deployment values, including quotes, comments, line endings and unrelated non-UTF-8 bytes. Atomically replace the env file and current symlink; each replacement is atomic individually, **not** a joint transaction.
6. Start with an exact image-ID Compose overlay and `up -d --no-build --pull never --no-deps --force-recreate --wait --wait-timeout 180 app`. Verify loopback readiness, OCI revision/image ID, current metadata, actual runtime configuration/digest, exact counts reconciliation, identity continuity, cold files, schema and absence of a gzip representation transition. A count decrease **or unexplained increase**, generation change, or newly active work refuses `active`. Failure still never restores a business/auth database.

Cold immutable SQLite reads are allowed only after checking for nonempty WAL/rollback journals. Such files cause a fail-closed review requirement; the operator does not checkpoint, delete or ignore them. It copies sidecars, but will not claim a consistent immutable SQL view with uncheckpointed transactions.

### Cold checkpoint is not code rollback

On an activation exception the operator tries **code/config-only rollback**: stop the scoped app, prove no schema/gzip transition, restore only the old environment/current pointer, and restart the retained old image ID without build/pull. **The current business and authentication databases are never replaced, even if credentials changed after the cold checkpoint.** A failed recreate that removed the container entirely is handled only after proving scoped service absence.

Unknown deployment/config drift, a changed schema/representation, lock conflict, unavailable image/daemon, or unsafe WAL condition can refuse automatic rollback. `phase.json` then says `manual_recovery_required`; do not restart incompatible old code or copy checkpoint `state.sqlite3` over the current DB. The checkpoint is evidence for a separately reviewed recovery, not automatic authorization to revive revoked credentials.

SIGTERM/ordinary errors attempt the guarded rollback; SIGKILL, host loss, or power failure cannot. Any non-`prepared` partial journal blocks a blind `activate` retry. Inspect journal phase and actual env/current/container state; do not erase a journal to bypass this gate. This CLI does not provide a general post-success rollback command.

### Verify and the next storage-migration phase

Standalone `verify` applies the same runtime-configuration and cold/live counts gates; it cannot accept merely because the container is healthy or an older activation receipt exists. It reports current metadata, exact image ID/revision, loopback readiness, the bound runtime digest, reconciled counts (`business_counts_unchanged`), identity counts and equality, checkpoint verification and its manifest hash. It never prints credentials or record contents. Later legitimate business writes, login/device heartbeat/session changes, or expired-session cleanup can make strict verification fail without modifying or stopping the live app. A prior `active` phase records a past successful activation, not a waiver of a later verification failure. Counts do not establish byte/content equality of every business row.

**Every new checkpoint adds artifact hardlinks.** Give the independent migration tool all participating roots: live `data`, the older retained cold data tree, and **every new `storage-upgrade-*/data` tree, including partial failed checkpoints**. Do not omit an alias just because its deployment failed. `migration_additional_data_root` is emitted and recorded in the cold manifest.

The completed ZIP hardlink is a same-disk alias, **not an off-host or independent disaster backup**. This operator creates no such backup. Compression is a separate, explicitly authorized step. After a migration rewrites artifact names/inodes, this operator's pre-migration cold manifest and rollback guard are intentionally no longer a valid post-migration verification/rollback contract; retain the migration tool's separate evidence and review restoration of raw compatibility before considering old code.

## Tests (no production contact)

The test module uses scratch SQLite/filesystem fixtures and a fake Docker protocol. Docker itself is only needed if running the suite in an isolated container. On Linux with a real POSIX scratch filesystem:

```sh
PYTHONDONTWRITEBYTECODE=1 HERMES_TEST_SCRATCH=/your/private/scratch \
  python3 tests/deploy/test_storage_upgrade.py
```

Run as root in an isolated test container to include root-owner/mode tests. Windows skips POSIX cases; WSL's `/mnt/e` filesystem may lack FIFO/Unix mode semantics. Those skips are **not** Linux acceptance. This local container command exercises Linux permissions/FIFO/hardlinks without mounting a Docker socket or production data:

```sh
docker run --rm --pull=never --user 0:0 --network none --read-only \
  --tmpfs /scratch:rw,mode=1777,size=1073741824 \
  -e PYTHONDONTWRITEBYTECODE=1 -e HERMES_TEST_SCRATCH=/scratch -e TMPDIR=/scratch \
  --mount type=bind,source=/mnt/d/website,target=/source,readonly \
  --entrypoint python linkarium:v0.1.2 /source/tests/deploy/test_storage_upgrade.py
```

Use the actual local source mount and an already-installed Python 3.11+ image. The reference image above was available locally during implementation; it is not the deployment target. TDD runs included observed RED cases for missing phases, unsafe archive acceptance/drift, source drift, restrictive-umask code permissions, missing-container rollback, unexpected schema changes and no-follow hashing before their GREEN fixes. A separate local real-Docker proof exercised the exact image-ID/legacy/offline overlay pattern and verified old code directories were removed and the final UID was 10001. Neither those fixtures nor the unit suite prove production deployment or real-user credential login; the parent operator owns that final acceptance.

The configuration/count repair adds RED controls for pre-existing runtime auth/port drift and startup bookmark loss; their original failures remain retained separately from GREEN logs. The scoped suite also covers prepared-digest drift, a second pre-stop check, image defaults, valid code metadata updates, missing/duplicate/extra configured values, prospective configuration changes, cold/active count reconciliation, independent `verify`, no database rewind, and synthetic secret sentinels across success/failure receipts and logs. Separate local Linux controls exercise actual Docker inspect/Compose resolution and the fixed baseline supervisor with synthetic initialized databases, real locks/cold copies and actual loopback readiness, including a loss/refusal/code-only-rollback case. They are not production authentication acceptance or a real target-release deployment.
