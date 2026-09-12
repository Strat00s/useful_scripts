#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


BY_DIRS = (
    "/dev/disk/by-id",
    "/dev/disk/by-uuid",
    "/dev/disk/by-label",
    "/dev/disk/by-partuuid",
    "/dev/disk/by-partlabel",
    "/dev/mapper",
)

# Filesystem/container types for which a normal statvfs() usage number is
# either unavailable or not meaningful as "user data usage".
NON_FILESYSTEM_TYPES = {
    "LVM2_member",
    "crypto_LUKS",
    "linux_raid_member",
    "swap",
    "squashfs",
    "zfs_member",
}


def check_root():
    if os.geteuid() != 0:
        sys.stderr.write("Error: root privileges required. Run with sudo.\n")
        sys.exit(1)


def run(cmd):
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None


def realpath(path):
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def human_bytes(value):
    if value is None:
        return "?"

    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)

    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    n = 0
    while abs(value) >= 1024 and n < len(units) - 1:
        value /= 1024.0
        n += 1

    if n == 0:
        return f"{int(value)} {units[n]}"
    if value >= 100:
        return f"{value:.0f} {units[n]}"
    if value >= 10:
        return f"{value:.1f} {units[n]}"
    return f"{value:.2f} {units[n]}"


def normalize_mount_path(value):
    # mountinfo uses octal escapes for spaces, tabs, and backslashes.
    return (
        value
        .replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\134", "\\")
    )


def get_lsblk():
    if not shutil.which("lsblk"):
        return []

    res = run([
        "lsblk",
        "-J",
        "-b",
        "-o",
        "NAME,KNAME,PKNAME,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,PARTLABEL,MOUNTPOINTS",
    ])

    if res is None or res.returncode != 0:
        return []

    try:
        return json.loads(res.stdout).get("blockdevices", [])
    except json.JSONDecodeError:
        return []


def flatten(nodes, parent=None, out=None):
    if out is None:
        out = []

    for node in nodes:
        node["_parent"] = parent
        out.append(node)
        for child in node.get("children") or []:
            flatten([child], node, out)

    return out


def get_by_links():
    refs = defaultdict(list)

    for directory in BY_DIRS:
        if not os.path.isdir(directory):
            continue

        try:
            for name in sorted(os.listdir(directory)):
                path = os.path.join(directory, name)
                if os.path.islink(path):
                    refs[realpath(path)].append(path)
        except OSError:
            pass

    return refs


def get_mounts():
    mounts = defaultdict(list)

    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return mounts

    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            fields = left.split()
            post = right.split()

            if len(fields) < 5 or len(post) < 2:
                continue

            mountpoint = normalize_mount_path(fields[4])
            source = normalize_mount_path(post[1])

            if source.startswith("/"):
                source = realpath(source)

            mounts[source].append(mountpoint)
        except (ValueError, IndexError):
            continue

    return mounts


def build_fstab_key_map(nodes):
    """Map UUID/LABEL/PARTUUID/PARTLABEL values to real device paths."""
    result = {}

    for node in nodes:
        kname = node.get("kname") or node.get("name")
        if not kname:
            continue

        device = realpath("/dev/" + kname)

        for key, field in (
            ("UUID=", "uuid"),
            ("LABEL=", "label"),
            ("PARTUUID=", "partuuid"),
            ("PARTLABEL=", "partlabel"),
        ):
            value = node.get(field)
            if value:
                result[key + str(value)] = device

    return result


def get_fstab(nodes):
    refs = defaultdict(list)
    identifier_map = build_fstab_key_map(nodes)

    try:
        with open("/etc/fstab", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return refs

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        source = normalize_mount_path(parts[0])
        mountpoint = normalize_mount_path(parts[1])

        if source.startswith("/dev/"):
            key = realpath(source)
        else:
            key = identifier_map.get(source)
            if key is None:
                continue

        refs[key].append(mountpoint)

    return refs


def get_runtime_status(disk):
    """
    Non-invasive spin-state check.

    Reading runtime_status is a sysfs read and does not send a command to the
    drive. We deliberately do NOT use sdparm/sg_requests as a fallback because
    SCSI commands can wake a sleeping drive on some stacks.
    """
    path = Path(f"/sys/block/{disk}/device/power/runtime_status")

    try:
        status = path.read_text().strip().lower()
    except OSError:
        return "UNKNOWN"

    if status == "active":
        return "SPINNING"
    if status == "suspended":
        return "SPUN DOWN"
    return "UNKNOWN"


def get_temperature(dev_path):
    """
    Read temperature only after the drive was already observed spinning.
    """
    if not shutil.which("smartctl"):
        return "-"

    res = run(["smartctl", "-A", dev_path])
    if res is None:
        return "-"

    output = res.stdout

    # ATA SMART attribute tables commonly expose one of these names.
    names = (
        "Temperature_Celsius",
        "Airflow_Temperature_Cel",
        "Temperature_Case",
        "Temperature",
    )

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if not any(name in stripped for name in names):
            continue

        fields = stripped.split()
        # Typical line:
        # 194 Temperature_Celsius ... RAW_VALUE 33
        if fields and fields[-1].lstrip("-+").isdigit():
            return f"{fields[-1]} °C"

        # Some smartctl versions/devices put the temperature elsewhere in
        # the VALUE/WORST/RAW text. Prefer the last integer on the line.
        numbers = [x for x in fields if x.lstrip("-+").isdigit()]
        if numbers:
            return f"{numbers[-1]} °C"

    # SCSI smartctl output can contain a direct temperature line.
    for line in output.splitlines():
        lower = line.lower()
        if "temperature" in lower:
            fields = line.replace(":", " ").split()
            for token in fields[1:]:
                token = token.rstrip("cC")
                if token.lstrip("-+").isdigit():
                    return f"{token} °C"

    return "-"


def statvfs_usage(mountpoint):
    """Return (used_bytes, total_bytes) for a mounted filesystem."""
    try:
        st = os.statvfs(mountpoint)
    except OSError:
        return None

    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    used = max(0, total - free)
    return used, total


def get_mount_usage(node, mounts):
    """Use the first actual mount for the node and return its filesystem use."""
    kname = node.get("kname") or node.get("name")
    if not kname:
        return None

    node_path = realpath("/dev/" + kname)

    for mountpoint in mounts.get(node_path, []):
        usage = statvfs_usage(mountpoint)
        if usage is not None:
            return usage

    return None


def descendants(node, all_nodes):
    result = []
    wanted = {id(node)}
    changed = True

    while changed:
        changed = False
        for candidate in all_nodes:
            parent = candidate.get("_parent")
            if (
                parent is not None
                and id(parent) in wanted
                and id(candidate) not in wanted
            ):
                wanted.add(id(candidate))
                result.append(candidate)
                changed = True

    return result


def all_children_including_drive(drive, all_nodes):
    return [drive] + descendants(drive, all_nodes)


def node_device(node):
    kname = node.get("kname") or node.get("name")
    return "/dev/" + kname if kname else "?"


def node_aliases(node, by_links):
    path = realpath(node_device(node))
    return sorted(set(by_links.get(path, [])))


def node_refs(node, by_links, mounts, fstab):
    path = realpath(node_device(node))
    refs = []

    for alias in node_aliases(node, by_links):
        refs.append("alias=" + alias)

    for mountpoint in mounts.get(path, []):
        refs.append("mount=" + mountpoint)

    for mountpoint in fstab.get(path, []):
        refs.append("fstab=" + mountpoint)

    return refs


def filesystem_name(node):
    return node.get("fstype") or "-"


def partition_usage(node, mounts):
    usage = get_mount_usage(node, mounts)
    if usage is None:
        size = node.get("size")
        return f"? / {human_bytes(size)}"

    used, total = usage
    return f"{human_bytes(used)} / {human_bytes(total)}"


def drive_usage(drive, all_nodes, mounts):
    """
    Sum usage for unique mounted descendants with actual filesystem stats.

    The denominator is always the physical/whole-disk size. If nothing below
    the disk is mounted, the used side is shown as '?'.
    """
    disk_size = drive.get("size")
    children = all_children_including_drive(drive, all_nodes)

    total_used = 0
    found = False
    counted_sources = set()

    for node in children:
        if node is drive:
            continue

        fstype = node.get("fstype") or ""
        if fstype in NON_FILESYSTEM_TYPES:
            continue

        device = realpath(node_device(node))
        if device in counted_sources:
            continue

        usage = get_mount_usage(node, mounts)
        if usage is None:
            continue

        counted_sources.add(device)
        total_used += usage[0]
        found = True

    used = human_bytes(total_used) if found else "?"
    return f"{used} / {human_bytes(disk_size)}"


def print_table(drives, all_nodes, by_links, mounts, fstab):
    headers = [
        "DRIVE",
        "DRIVE ALIASES",
        "DRIVE USED/CAPACITY",
        "SPIN",
        "TEMP",
        "PARTITION",
        "PARTITION REFS",
        "PART USED/CAPACITY",
        "FILESYSTEM",
    ]

    rows = []

    for drive in drives:
        children = descendants(drive, all_nodes)
        # Show block-device descendants (partitions, dm/LVM children, etc.).
        # If a disk has no children, still emit one main row.
        child_nodes = [n for n in children if n.get("type") != "disk"]

        spin = get_runtime_status(drive.get("kname") or drive.get("name") or "")
        temp = get_temperature(node_device(drive)) if spin == "SPINNING" else "-"
        aliases = "; ".join(node_aliases(drive, by_links)) or "-"
        d_usage = drive_usage(drive, all_nodes, mounts)

        main_prefix = [
            node_device(drive),
            aliases,
            d_usage,
            spin,
            temp,
        ]

        if not child_nodes:
            rows.append(main_prefix + ["-", "-", "-", "-"])
            continue

        first = True
        for child in child_nodes:
            refs = "; ".join(node_refs(child, by_links, mounts, fstab)) or "-"
            part = node_device(child)
            p_usage = partition_usage(child, mounts)
            fs = filesystem_name(child)

            prefix = main_prefix if first else ["", "", "", "", ""]
            rows.append(prefix + [part, refs, p_usage, fs])
            first = False

    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(str(value)))

    # Keep the output readable on normal terminals. Long alias/reference
    # fields are truncated rather than making the whole table enormous.
    max_widths = [
        18, 44, 22, 12, 10, 20, 64, 24, 22,
    ]

    widths = [min(widths[i], max_widths[i]) for i in range(len(headers))]

    def fit(value, width):
        value = str(value)
        if len(value) <= width:
            return value
        return value[: max(1, width - 1)] + "…"

    separator = "-+-".join("-" * w for w in widths)
    print(" | ".join(fit(headers[i], widths[i]).ljust(widths[i]) for i in range(len(headers))))
    print(separator)

    for row in rows:
        print(" | ".join(fit(row[i], widths[i]).ljust(widths[i]) for i in range(len(row))))


def main():
    check_root()

    nodes = flatten(get_lsblk())
    if not nodes:
        print("Could not read block-device information with lsblk.")
        return 1

    by_links = get_by_links()
    mounts = get_mounts()
    fstab = get_fstab(nodes)

    # Whole-disk nodes. This naturally includes /dev/sdX, but also works for
    # other disk devices such as NVMe/SAS devices when present.
    drives = [node for node in nodes if node.get("type") == "disk"]

    if not drives:
        print("No block devices of type 'disk' found.")
        return 0

    print_table(drives, nodes, by_links, mounts, fstab)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
