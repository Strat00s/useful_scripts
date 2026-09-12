#!/usr/bin/env python3

import os
import subprocess
import sys


def get_xattr(path, attribute):
    """Read an extended attribute using getfattr."""
    try:
        result = subprocess.run(
            ["getfattr", "--only-values", "-n", attribute, path],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        error = e.stderr.strip()
        print(
            f"Error reading {attribute} from {path}: {error}",
            file=sys.stderr,
        )
        return None


def realpath(path):
    """Resolve symlinks such as /dev/disk/by-uuid/... -> /dev/sdc1."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def decode_mount_field(value):
    """
    Decode the escape sequences used in /proc/mounts.

    Examples:
        \\040 -> space
        \\011 -> tab
        \\134 -> backslash
    """
    result = bytearray()
    i = 0

    while i < len(value):
        if (
            value[i] == "\\"
            and i + 3 < len(value)
            and all(c in "01234567" for c in value[i + 1:i + 4])
        ):
            result.append(int(value[i + 1:i + 4], 8))
            i += 4
        else:
            result.extend(value[i].encode())
            i += 1

    return result.decode()


def get_mergerfs_mounts():
    """
    Discover all mounted mergerfs pools from /proc/mounts.

    Multiple mountpoints using the same mergerfs fsname are treated as
    one pool. All of that pool's mountpoints are retained.
    """
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"Unable to read /proc/mounts: {e}", file=sys.stderr)
        sys.exit(1)

    pools = {}

    for line in lines:
        parts = line.split()

        if len(parts) < 3:
            continue

        source = decode_mount_field(parts[0])
        mountpoint = decode_mount_field(parts[1])
        fstype = parts[2]

        if fstype != "fuse.mergerfs":
            continue

        # The mergerfs fsname is used as the pool identity.
        pool = pools.setdefault(
            source,
            {
                "fsname": source,
                "mountpoints": [],
            },
        )

        if mountpoint not in pool["mountpoints"]:
            pool["mountpoints"].append(mountpoint)

    return list(pools.values())


def get_df_info():
    """
    Run df -hP once and return filesystem information indexed by mountpoint
    and resolved device path.
    """
    df_info = {}

    try:
        result = subprocess.run(
            ["df", "-hP"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Unable to run df: {e}", file=sys.stderr)
        sys.exit(1)

    for line in result.stdout.splitlines()[1:]:
        # -P guarantees one filesystem per line.
        parts = line.split(maxsplit=5)

        if len(parts) != 6:
            continue

        device, size, used, avail, pct, mountpoint = parts

        info = {
            "device": device,
            "size": size,
            "used": used,
            "avail": avail,
            "pct": pct,
        }

        # Index by mountpoint.
        df_info[mountpoint] = info

        # Also index by resolved device path where applicable.
        if device.startswith("/"):
            df_info[realpath(device)] = info

    return df_info


def get_parent_disk(partition):
    """
    Return the parent /dev/sdX-style disk for a partition.

    Uses sysfs rather than lsblk.
    """
    if not partition.startswith("/dev/"):
        return "-"

    name = os.path.basename(partition)
    sys_block = "/sys/class/block"
    partition_sysfs = os.path.join(sys_block, name)

    try:
        device_path = os.path.realpath(
            os.path.join(partition_sysfs, "device")
        )

        current = os.path.dirname(device_path)

        while current and current != "/":
            basename = os.path.basename(current)
            candidate = os.path.join(sys_block, basename)

            if (
                os.path.exists(candidate)
                and os.path.exists(os.path.join(candidate, "dev"))
                and basename != name
            ):
                return f"/dev/{basename}"

            current = os.path.dirname(current)

    except OSError:
        pass

    return "-"


def format_branch(branch):
    """
    Parse:

        /srv/foo=RW

    into:

        ("/srv/foo", "RW")
    """
    if "=" in branch:
        path, mode = branch.rsplit("=", 1)
    else:
        path, mode = branch, "?"

    return path, mode


def print_table(rows):
    headers = (
        "BRANCH",
        "DRIVE",
        "PARTITION",
        "MODE",
        "CAPACITY",
        "USED",
        "FREE",
        "USE%",
    )

    if not rows:
        print("(no branches)")
        return

    widths = [
        max(
            len(headers[i]),
            *(len(str(row[i])) for row in rows),
        )
        for i in range(len(headers))
    ]

    print(
        f"{headers[0]:<{widths[0]}}  "
        f"{headers[1]:<{widths[1]}}  "
        f"{headers[2]:<{widths[2]}}  "
        f"{headers[3]:>{widths[3]}}  "
        f"{headers[4]:>{widths[4]}}  "
        f"{headers[5]:>{widths[5]}}  "
        f"{headers[6]:>{widths[6]}}  "
        f"{headers[7]:>{widths[7]}}"
    )

    print("-" * (sum(widths) + 2 * (len(headers) - 1)))

    for row in rows:
        print(
            f"{row[0]:<{widths[0]}}  "
            f"{row[1]:<{widths[1]}}  "
            f"{row[2]:<{widths[2]}}  "
            f"{row[3]:>{widths[3]}}  "
            f"{row[4]:>{widths[4]}}  "
            f"{row[5]:>{widths[5]}}  "
            f"{row[6]:>{widths[6]}}  "
            f"{row[7]:>{widths[7]}}"
        )


def show_pool(pool, df_info):
    """
    Show one unique mergerfs pool.

    If the same pool is mounted at multiple locations, all mountpoints
    are shown, but the pool itself is displayed only once.
    """
    mountpoints = pool["mountpoints"]

    # Any mountpoint belonging to the pool exposes the same mergerfs
    # metadata, so use the first one.
    metadata_mount = mountpoints[0]
    meta = os.path.join(metadata_mount, ".mergerfs")

    fsname = get_xattr(
        meta,
        "user.mergerfs.fsname",
    )

    branch_data = get_xattr(
        meta,
        "user.mergerfs.branches",
    )

    print()
    print(f"mergerfs: {fsname or pool['fsname']}")
    print(f"mounts:   {', '.join(mountpoints)}")
    print()

    if not branch_data:
        print("Unable to read mergerfs branch metadata.")
        return

    branches = [
        format_branch(branch)
        for branch in branch_data.split(":")
        if branch
    ]

    rows = []

    for branch, mode in branches:
        info = df_info.get(branch)

        if info is None:
            rows.append(
                (
                    branch,
                    "-",
                    "-",
                    mode,
                    "-",
                    "-",
                    "-",
                    "N/A",
                )
            )
            continue

        device = info["device"]

        if device.startswith("/"):
            device = realpath(device)

        drive = get_parent_disk(device)

        rows.append(
            (
                branch,
                drive,
                device,
                mode,
                info["size"],
                info["used"],
                info["avail"],
                info["pct"],
            )
        )

    print_table(rows)

    # Find the aggregate mergerfs filesystem in df.
    # There may be multiple entries because the same pool can be mounted
    # at multiple mountpoints, so try each mountpoint.
    mergerfs_info = None

    for mountpoint in mountpoints:
        mergerfs_info = df_info.get(mountpoint)

        if mergerfs_info:
            break

    print()
    print("ENTIRE MERGERFS")
    print("-" * 40)

    if mergerfs_info:
        print(f"Device:    {mergerfs_info['device']}")
        print(f"Capacity:  {mergerfs_info['size']}")
        print(f"Used:      {mergerfs_info['used']}")
        print(f"Free:      {mergerfs_info['avail']}")
        print(f"Use:       {mergerfs_info['pct']}")
    else:
        print("Not found in df output.")


def main():
    pools = get_mergerfs_mounts()

    if not pools:
        print("No mergerfs pools found.")
        return

    # Run df exactly once for all pools.
    df_info = get_df_info()

    for pool in pools:
        show_pool(pool, df_info)


if __name__ == "__main__":
    main()