# useful_scripts

Personal management and health-check scripts for a Linux storage box
(SATA/SAS/NVMe drives behind mergerfs). Every script is standalone: it talks to
the system through `lsblk`, `smartctl`, `sysfs`, `/proc`, `/etc/fstab` and
`df` — no config files, no arguments needed for the common case, and no
dependency on the other scripts here (a little helper duplication is on
purpose, so any single file can be copied to another machine and just run).

All of them are read-only **except** `smart/drive_smart_test.py`, which starts
SMART self-tests on the drives.

## Scripts

| Script | Question it answers | Touches drives? | Root |
|---|---|---|---|
| `inventory/check_sleep.py` | Which disks are asleep right now? | probes power state | required |
| `inventory/drive_stats.py` | Wide one-glance table: state, temp, partitions, usage, aliases | no | required |
| `inventory/drive_usage.py` | Same idea as `drive_stats.py` in a compact `|` table with real filesystem usage | no (sysfs-only spin check) | required |
| `inventory/drive_topology.py` | Which device node / alias / UUID / fstab entry belongs to which disk? | no | not needed |
| `pools/mergerfs_status.py` | Which branches make up each mergerfs pool, and how full is each? | no | not needed |
| `smart/drive_smart_status.py` | SMART health, temperature, power-on hours, self-test history | reads SMART | recommended |
| `smart/drive_smart_test.py` | Run short/extended self-tests on every drive that supports them and report | **yes** | required |

## Requirements

* Linux, Python 3.9+ (standard library only)
* `smartmontools` (`smartctl`) — SMART scripts, temperature and spin checks
* `util-linux` (`lsblk`) — block-device discovery
* `attr` (`getfattr`) — mergerfs pool metadata
* `sdparm` and/or `sg3_utils` (`sg_requests`) — optional spin-state fallbacks
* `df` (coreutils) — capacity/usage columns

Debian/Ubuntu: `apt install smartmontools util-linux attr sdparm sg3-utils`

## inventory/

### `check_sleep.py`

Prints one line per disk: `DEVICE  MODEL  STATUS  METHOD`, where `STATUS` is
`SPINNING`, `SPUN DOWN` or `UNKNOWN / ERR` and `METHOD` names the probe that
actually answered, so you can see which tool on the box is trustworthy.

The state is resolved with a fallback chain: `smartctl -i -n standby` (its exit
code 0/2 *is* the answer and the `-n standby` guard means it will not spin a
spun-down drive), then `sdparm --command=sense`, then `sg_requests` (SCSI sense
code: "power condition"/"not ready" vs "no additional sense"), and finally
`/sys/block/<disk>/device/power/runtime_status`.

Scans `/sys/block/sd*`, so it covers SATA/SAS disks only — NVMe and mmcblk
devices are not listed. Caveat: while `smartctl -n standby` is safe, the
`sdparm`/`sg_requests` probes send real SCSI commands, which can wake a
sleeping drive on some stacks; `drive_usage.py` exists partly to avoid that.

### `drive_stats.py`

The wide report (148 columns wide) — one row per partition, grouped under its
drive:

```
DRIVE ID  STATE  TEMP  DRIVE CAPACITY  PARTITION  FS TYPE  PART CAPACITY  ALIASES / FSTAB / MOUNT POINTS
```

Combines spin state (same four-method chain as `check_sleep.py`), temperature
(read only when the drive is already confirmed spinning, so no cold reads),
drive/partition sizes from `lsblk -J`, per-partition `used / total` from
`df -hP` with an `lsblk` size fallback when a partition is unmounted, and the
references each node is known by: live mounts from `/proc/self/mountinfo`,
`/etc/fstab` entries (including `UUID=`/`LABEL=` sources) and
`/dev/disk/by-*` + `/dev/mapper` symlinks.

### `drive_usage.py`

Compact `|`-separated table, best when you want to read or diff the output:

```
DRIVE | DRIVE ALIASES | DRIVE USED/CAPACITY | SPIN | TEMP | PARTITION | PARTITION REFS | PART USED/CAPACITY | FILESYSTEM
```

Differences worth knowing:

* Usage comes from `os.statvfs()` on a real mountpoint rather than parsing
  `df`. Container filesystem types where "used" is meaningless (`LVM2_member`,
  `crypto_LUKS`, `linux_raid_member`, `swap`, `squashfs`, `zfs_member`) are
  excluded from the numbers instead of reported as full.
* Drive-level usage is the sum of its children against the whole-disk size.
* Spin state is a pure `sysfs` read — deliberately **no** `sdparm`/`sg_requests`
  fallback, because those SCSI commands can wake a sleeping drive.
* Temperature is queried only for drives already seen as `SPINNING`.

### `drive_topology.py`

Reference map, printed as one block per whole disk:

* `type`, `size`, `filesystem`, `label`, `uuid`, `partuuid`
* aliases pointing at the disk itself (`/dev/disk/by-id`, `by-uuid`, `by-label`,
  `by-partuuid`, `by-partlabel`, `/dev/mapper`)
* the full descendant tree (partitions, dm/LVM children) with each node's
  `mounted:`, `fstab:` and `alias:` lines
* a trailing section of sources that are not disks (tmpfs, NFS, overlay, …)

Use this when you need to be sure that `/dev/sdd1`,
`/dev/disk/by-uuid/…` and an fstab line are the same piece of hardware.
Read-only, no root needed.

## pools/

### `mergerfs_status.py`

Finds mergerfs mounts by scanning `/proc/mounts` for `fuse.mergerfs`, dedupes
pools by fsname (a pool mounted at several mountpoints is shown once, with all
of its mountpoints listed), then reads the pool's own metadata from the
`user.mergerfs.fsname` and `user.mergerfs.branches` extended attributes of
`<mount>/.mergerfs`.

Per pool it prints a table of every branch — `BRANCH`, `DRIVE`, `PARTITION`,
`MODE` (the `RW`/`RO` suffix from the branch spec), `CAPACITY`, `USED`, `FREE`,
`USE%` — where the branch path is resolved through `realpath` and
`/sys/class/block` down to the partition and its parent disk, followed by an
`ENTIRE MERGERFS` block with the pooled totals from a single `df -hP` run.

## smart/

### `drive_smart_status.py`

Read-only SMART report. Discovers devices with `smartctl --scan --json`, then
`smartctl -a --json` per device.

Summary table first — `DEVICE`, `MODEL`, `STATUS`, `TEMP`, `POWER-ON`, `ALERTS`
— then a detail card per drive: serial number, interface, SMART pass/fail
assessment, power-on lifetime, temperature, self-test statistics (lifetime
count, short vs extended split, last test status, and the recorded failure
entries), health indicators, and a critical-alerts block. Alerts are raised
from ATA attributes 5 (reallocated sectors), 187 (reported uncorrectable),
197 (current pending), 198 (offline uncorrectable), and from NVMe critical
warning, media errors and percentage-used wear. Warns, but does not stop, when
run without root — some of that data needs it.

### `drive_smart_test.py`

The only state-changing script: it launches SMART self-tests, waits for them to
finish, and prints a report. Exits `0` when everything launched completed
without error, `1` when anything failed / aborted / timed out, `2` when
prerequisites are missing (no `smartctl`, no root).

```
sudo ./smart/drive_smart_test.py -t short              # all drives, concurrently
sudo ./smart/drive_smart_test.py -t long -s -p 60      # one drive at a time, 60s polls
sudo ./smart/drive_smart_test.py -t short -d /dev/sda /dev/sdc -c | less -R
```

| Flag | Meaning |
|---|---|
| `-t`, `--test` | `short`, `long` or `extended` (required) |
| `-d`, `--drives` | restrict to these devices; repeatable |
| `-p`, `--poll` | poll interval in seconds while tests run (default 15) |
| `-s`, `--sequential` | fully test one drive before starting the next |
| `-c`, `--color` | force ANSI colors even when output is piped/redirected |

Behaviour notes: drives come from `lsblk` unioned with the `smartctl` scan;
each candidate is probed for self-test support, and virtual / QEMU / virtio /
hardware-RAID-passthrough disks that do not implement self-tests are reported
as `SKIPPED` rather than as failures. Completion is read back from the
self-test log (ATA, SCSI and NVMe log shapes are all handled), with a timeout
derived from `smartctl`'s own estimated polling time for drives that never
update the log. A plan table is printed before anything starts, results after,
plus a post-test SMART health check (status, logged test failures, power-on
hours). `Ctrl-C` sends `smartctl -X` to every in-flight test before exiting.

## Output note

These scripts print drive serial numbers, filesystem UUIDs, volume labels and
local mount paths. That is the point when you are debugging your own box, but
scrub those from any output you paste into an issue tracker or a forum.

Colors follow tty detection: piping or redirecting produces plain text.
`drive_smart_test.py -c` overrides that when you want colors through `less -R`.

## License

GPL-3.0 — see [LICENSE](LICENSE).
