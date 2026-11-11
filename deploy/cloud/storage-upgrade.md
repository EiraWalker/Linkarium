# Storage-compatible code upgrade operator

`storage_upgrade.py` provides a **one-host, code-only upgrade and guarded code rollback**, not data migration or disaster recovery. It does not change Nginx, initialize authentication, execute migrations, compress/decode artifacts, restore a database, pull an image, or install dependencies.

## Scope and prerequisites

- Run with Linux root and Python 3.11+ on the source host. `BASE` is fixed at `/srv/linkarium`; there is no base-path override. Docker is explicitly addressed through the local Unix socket, not inherited Docker contexts.
- Existing layout: `current` is a symlink to a direct child of `releases/`; `production.env` is root-owned `0600` with a single hardlink; `data/`, `releases/`, and `ops/` already exist. Base/config parents must be root-owned and not group/world-writable.
- This bounded operator expects the existing single-service `linkarium` Compose project, `linkarium-app-1`, the canonical cloud Compose file, one bind mount from `/srv/linkarium/data` to `/data`, and one `127.0.0.1` HTTP publication. Capture overlays or other deployment variants fail closed; do not silently remove their mounts/configuration to satisfy this operator.
- Use a committed package produced by `scripts/build-deploy.py`. Obtain its SHA-256 and complete commit through the approved release/build workflow. A package's own metadata is not an independent signature. Keep uploaded packages and operator code under root control and do not edit them while a phase runs.
- The existing image must have matching version/revision labels and matching application file hashes. Requirements, canonical Dockerfile, Compose file, and migration-file sets/hashes must match old vs new release. Missing/undeclared extra image source files also fail the source proof. Only the exact old-base inventory below has a declared legacy supplement; target images never use it.
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

#### Pinned legacy base inventory

The original production preparation **failed correctly** with `DOCKER_COMMAND_FAILED`: the unchanged `IMAGE_CHECK` found 124 application files against 123 expected, with no missing or changed files and exactly one extra public JavaScript asset. `storage2-prepare-diagnostic-v1-output.txt` records that refusal. The separate read-only `legacy-asset-provenance-v1-output.txt` established the extra's identical path, size and SHA-256 in retained `v0.1.2`, `v0.1.3-firefox.1` and `v0.1.3-firefox.2` releases, and no literal reference to that path in the current release. This is a declared old static-resource residue, not user data; the old assertion is not reclassified as having passed.

`LEGACY_BASE_IMAGE` binds the supplement to **all** of these identities:

| Binding | Exact value |
| --- | --- |
| Immutable old image ID | `sha256:9678767011890258ddd0d24460c6976cf479aa35cb23f1d3761676437de0c5c5` |
| Old release | `/srv/linkarium/releases/Linkarium-v0.2.0-local-ai.1-deploy` |
| Version / commit | `v0.2.0-local-ai.1` / `32b1b857b1ba3dc7b5835a5e750cef2ef4ed8576` |
| Complete `SHA256SUMS` SHA-256 | `3b14035e756c0b4b6af0503fa4c5602f37781b1e5706f5cb9789506cfb1ca79a` |
| Sole supplemental path | `frontend/dist/assets/index-D4nBYapU.js` |
| Supplemental SHA-256 | `c4d161304e3084502915f49e7e989106b643e01729006b28beb6eceb13c5d587` |
| Observed supplemental size | 167117 bytes |

Only `prepare`'s old-base source check supplies `base_journal`. For the pinned image it requires the exact release path and manifest digest, then verifies the complete release, including the manifest-bound `release.json` metadata. It adds the single declared path/hash without replacing any release entry. The original probe still walks and hashes the entire application inventory and requires **`actual == complete_expected`**, now 123 release entries plus the one required supplemental entry. An altered/missing legacy file, second extra, missing/changed original file, wrong release or changed manifest is refused. Other image IDs retain exact release-only comparison; a matching-looking extra is not accepted on another image. There is no wildcard ignore, skip flag, environment override or configurable inventory input.

All target/new-image source checks remain release-only, including when the same pinned ID is passed to the ordinary checker. The offline Dockerfile still removes the old application directories before copying target code, so the residue must not survive into the target. No Docker resource/network/dependency, runtime environment, business/auth data, activation or rollback rules change.

After a successful old-base probe, root-private `base-source-proof.json` retains the complete checked path/hash map, source digest, immutable image ID, release path and manifest digest. Its `legacy_inventory` is the explicit fixed identity/supplement/provenance record above, or `null` for ordinary bases. `prepared.json` records this file's `base_source_proof_sha256`; `old_source_sha256` is the digest of the complete checked inventory, not the release-only subset. The existing `old_metadata` and manifest bindings remain. A proof-file write failure prevents preparation; a base proof can remain after a later target failure and **does not itself mean preparation succeeded**. Existing failed journals are still retained for inspection, never silently reused or erased.

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

SIGTERM/ordinary activation errors attempt the guarded automatic rollback; SIGKILL, host loss, or power failure cannot. Any non-`prepared` partial journal blocks a blind `activate` retry. Inspect journal phase and actual env/current/container state; do not erase a journal to bypass this gate. The explicit command below is limited to a successfully activated, receipt-bound upgrade, not arbitrary deployments or partial recovery.

### Explicit post-activation rollback

This is a separate, deliberately requested action, **not the next step of every upgrade**:

```sh
python3 "$OP" rollback \
  --version "$VERSION" --commit "$COMMIT" --expected-base "$EXPECTED_BASE"
```

Use the same target version, target commit and original expected-base commit as `prepare`/`activate`; do **not** swap the hashes or pass the old version as `--version`. Package arguments remain forbidden. The old release/image must still be retained. Only journal phase `active` with a matching successful `activation-receipt.json` is eligible. Prepared, starting, already-rolled-back, unknown and interrupted/manual-recovery phases fail closed; there is no blind retry, force flag or idempotent-success shortcut.

Before any stop, the command reuses the existing release/manifests, pinned images, active container scope/health, exact env bytes and runtime-configuration bindings. The prospective old image configuration must match the prepared normalized digest as well. It checks current identity inputs, idle jobs/maintenance and the existing schema/representation guard, then repeats the active code/config checks after the potentially slow data scan. A pre-stop refusal leaves the service and phase unchanged.

**A compressed active library is refused, even if gzip was already recorded in the checkpoint guard.** No implicit decompression or database restoration occurs. This command neither implements nor authorizes all-root raw recovery, its cumulative space plan or the separate full-library acceptance gate.

The command reuses `rollback_code()` to stop only the bound app, acquire the real supervisor lock, recheck the current/env bindings and schema/storage guard, and require quiescent SQLite and idle state. Actual network publications can disappear when the container stops; the unchanged full running-container configuration gate runs before stop and after restart, not on that stopped network state. Under the supervisor lock it records **fresh current** `rollback-counts-before.json` and `rollback-identity-before.json`. Newer business rows, revoked credentials and newer sessions since activation are preserved; historical cold counts/auth summaries are not the continuity baseline. The old cold database and physical manifest are not read to satisfy these checks, and standalone `verify` is not called.

Only the environment's two deployment values, the `current` pointer and the running code image are reverted, using the retained byte-exact env checkpoint and old pinned image. The two atomic file/pointer replacements are not a joint transaction. No current business/auth database or artifact is copied, replaced, deleted or rewound. Restart uses the existing no-build/no-pull pinned overlay. Successful postchecks require the old pinned healthy container, loopback readiness, original env bytes/current target, exact runtime configuration, unchanged schema/representation, and fresh pre/post identity/count equality. Concurrent legitimate writes or session-expiry cleanup still cause conservative refusal; counts do not prove every business row's content equality.

`rolling_back` is written before the stop attempt. Restart, lock cleanup, write or postcheck failures retain a non-success phase (`manual_recovery_required`, or `rolling_back` if failure prevents updating the journal) and a failed CLI outcome. There is no second automatic code switch or data recovery attempt. The app may be stopped or running the old image when failure is reported: inspect it rather than assuming either state. If journal storage itself is unavailable, the nonzero CLI outcome must not be overridden by stale phase/receipt bytes.

Acceptance requires **all three**: exit zero with the final `complete` event, `phase.json` equal to `code_rolled_back`, and `rollback-receipt.json` from that attempt. A receipt alone is not success: final phase publication or operator-lock cleanup can still fail afterward. The completion event is emitted only after operator-lock cleanup. Output contains booleans, counts and digests (including a generation digest), not credentials, titles, URLs or rows. Keep all historical checkpoints/journals; they are not automatically cleaned up.

### Verify and the next storage-migration phase

Standalone `verify` applies the same runtime-configuration and cold/live counts gates; it cannot accept merely because the container is healthy or an older activation receipt exists. It reports current metadata, exact image ID/revision, loopback readiness, the bound runtime digest, reconciled counts (`business_counts_unchanged`), identity counts and equality, checkpoint verification and its manifest hash. It never prints credentials or record contents. Later legitimate business writes, login/device heartbeat/session changes, or expired-session cleanup can make strict verification fail without modifying or stopping the live app. A prior `active` phase records a past successful activation, not a waiver of a later verification failure. Counts do not establish byte/content equality of every business row.

**Every new checkpoint adds artifact hardlinks.** Give the independent migration tool all participating roots: live `data`, the older retained cold data tree, and **every new `storage-upgrade-*/data` tree, including partial failed checkpoints**. Do not omit an alias just because its deployment failed. `migration_additional_data_root` is emitted and recorded in the cold manifest.

The completed ZIP hardlink is a same-disk alias, **not an off-host or independent disaster backup**. This operator creates no such backup. Compression is a separate, explicitly authorized step. After a migration rewrites artifact names/inodes, this operator's pre-migration cold manifest and rollback guard are intentionally no longer a valid post-migration verification/rollback contract; retain the migration tool's separate evidence and review restoration of raw compatibility before considering old code.

## Tests (no production contact)

The test modules use scratch SQLite/filesystem fixtures and a fake Docker protocol. Docker itself is only needed if running the suite in an isolated container. On Linux with a real POSIX scratch filesystem:

```sh
PYTHONDONTWRITEBYTECODE=1 HERMES_TEST_SCRATCH=/your/private/scratch \
  python3 tests/deploy/test_storage_upgrade.py

PYTHONDONTWRITEBYTECODE=1 HERMES_TEST_SCRATCH=/your/private/scratch \
  python3 tests/deploy/test_storage_rollback.py

PYTHONDONTWRITEBYTECODE=1 HERMES_TEST_SCRATCH=/your/private/scratch \
  python3 tests/deploy/test_storage_legacy_image.py
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

The explicit-rollback suite imports the unchanged original fixtures and enters through the CLI. It exercises real env/current/SQLite effects, fresh data newer than the cold checkpoint, receipt/phase/config/image refusals before stop, gzip/schema/maintenance races, post-start continuity failures, actual cross-process POSIX lock conflict, cleanup/publication failures and privacy sentinels. Docker and readiness responses are mocked here; native Linux tmpfs acceptance is a filesystem/locking proof, **not** real-Docker or production rollback acceptance. Keep Windows skips explicit and run both modules; do not replace the original operator suite with the new tests.

The legacy-image suite additionally executes the **unchanged `IMAGE_CHECK` source** in fresh Python subprocesses over real small scratch file trees. Only `/app` path resolution is relocated (with POSIX relative-name spelling on Windows); walks, file types, SHA-256 and exact equality are not mocked or filtered. Synthetic baseline pins are explicit fixtures, not claims about a production image. Coverage includes the declared-base positive, complete persisted proof, altered supplemental hash, second extra, missing/changed originals, required supplement, wrong image/release/metadata/manifest, overlap refusal, strict target rejection and proof-write failure. Docker inspect/build/Compose remain protocol mocks; an isolated container running these tests is not a real overlay build or production acceptance. Preserve both observed TDD RED logs and GREEN results, and retain all 93 original operator/rollback methods. For Windows runs set `HERMES_TEST_SCRATCH`, `TEMP`, `TMP` and `TMPDIR` explicitly to an owned scratch directory; POSIX skips still require the native Linux tmpfs run above.
