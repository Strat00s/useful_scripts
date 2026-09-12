#!/usr/bin/env python3

import json
import os
import re
import subprocess
from collections import defaultdict


BY_DIRS = (
    "/dev/disk/by-id",
    "/dev/disk/by-uuid",
    "/dev/disk/by-label",
    "/dev/disk/by-partuuid",
    "/dev/disk/by-partlabel",
    "/dev/mapper",
)


def run(cmd):
    try:
        return subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def realpath(path):
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def get_lsblk():
    """
    Get the block-device tree from lsblk.
    """
    raw = run([
        "lsblk",
        "-J",
        "-o",
        "NAME,KNAME,PKNAME,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINTS",
    ])

    if not raw:
        return []

    try:
        return json.loads(raw).get("blockdevices", [])
    except json.JSONDecodeError:
        return []


def flatten(nodes, parent=None, out=None):
    """
    Flatten the lsblk tree while retaining parent relationships.
    """
    if out is None:
        out = []

    for node in nodes:
        node["_parent"] = parent
        out.append(node)

        for child in node.get("children") or []:
            flatten([child], node, out)

    return out


def get_by_links():
    """
    Map real device paths to all symlinks pointing at them, e.g.

        /dev/sda1
          -> /dev/disk/by-uuid/...
          -> /dev/disk/by-partuuid/...
          -> /dev/disk/by-id/...
    """
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
    """
    Read /proc/self/mountinfo and return:

        device -> [mountpoint, ...]
    """
    mounts = defaultdict(list)

    try:
        lines = open(
            "/proc/self/mountinfo",
            encoding="utf-8",
        ).read().splitlines()
    except OSError:
        return mounts

    for line in lines:
        try:
            left, right = line.split(" - ", 1)

            fields = left.split()
            post = right.split()

            mount_point = bytes(
                fields[4],
                "utf-8",
            ).decode("unicode_escape")

            source = post[1] if len(post) > 1 else "?"

            if source.startswith("/") and os.path.exists(source):
                key = realpath(source)
            else:
                key = source

            mounts[key].append(mount_point)

        except (ValueError, IndexError):
            continue

    return mounts


def unescape_fstab(value):
    return (
        value
        .replace(r"\040", " ")
        .replace(r"\011", "\t")
    )


def get_fstab():
    """
    Read /etc/fstab and map devices/UUID references to mountpoints.
    """
    refs = defaultdict(list)

    try:
        lines = open(
            "/etc/fstab",
            encoding="utf-8",
        ).read().splitlines()
    except OSError:
        return refs

    for line in lines:
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        parts = re.split(r"\s+", line)

        if len(parts) < 2:
            continue

        source = unescape_fstab(parts[0])
        mountpoint = unescape_fstab(parts[1])

        if source.startswith("/dev/"):
            key = realpath(source)
        else:
            key = source

        refs[key].append(mountpoint)

    return refs


def print_drive(drive, all_nodes, by_links, mounts, fstab):
    name = drive.get("kname") or drive.get("name") or "?"
    dev = "/dev/" + name

    print()
    print("=" * 88)
    print(dev)

    print(f"  type       : {drive.get('type', '-')}")
    print(f"  size       : {drive.get('size', '-')}")
    print(f"  filesystem : {drive.get('fstype') or '-'}")
    print(f"  label      : {drive.get('label') or '-'}")
    print(f"  uuid       : {drive.get('uuid') or '-'}")
    print(f"  partuuid   : {drive.get('partuuid') or '-'}")

    # ------------------------------------------------------------
    # Aliases pointing to the physical disk itself
    # ------------------------------------------------------------

    aliases = sorted(
        set(by_links.get(realpath(dev), []))
    )

    print()
    print("  aliases (/dev/disk/by-* and /dev/mapper):")

    if aliases:
        for alias in aliases:
            print(f"    {alias} -> {realpath(alias)}")
    else:
        print("    -")

    # ------------------------------------------------------------
    # Find all descendants (partitions, LVs, etc.)
    # ------------------------------------------------------------

    drive_nodes = {id(drive)}

    changed = True

    while changed:
        changed = False

        for node in all_nodes:
            parent = node.get("_parent")

            if (
                parent is not None
                and id(parent) in drive_nodes
                and id(node) not in drive_nodes
            ):
                drive_nodes.add(id(node))
                changed = True

    nodes = [
        node
        for node in all_nodes
        if id(node) in drive_nodes
    ]

    print()
    print("  partitions / children:")

    for node in nodes:
        n = node.get("kname") or node.get("name") or "?"
        path = "/dev/" + n

        if node is drive:
            prefix = "    drive"
        else:
            prefix = "    part "

        info = [
            path,
            node.get("size") or "",
            node.get("fstype") or "",
            node.get("uuid") or "",
        ]

        print(
            f"{prefix:<10}: "
            + "  ".join(x for x in info if x)
        )

        node_path = realpath(path)

        # Actual mounts
        for mountpoint in mounts.get(node_path, []):
            print(f"             mounted: {mountpoint}")

        # /etc/fstab references
        for mountpoint in fstab.get(node_path, []):
            print(f"             fstab   : {mountpoint}")

        # Persistent /dev/disk/by-* aliases
        for alias in sorted(
            set(by_links.get(node_path, []))
        ):
            print(f"             alias   : {alias}")


def main():
    nodes = flatten(get_lsblk())
    by_links = get_by_links()
    mounts = get_mounts()
    fstab = get_fstab()

    drives = [
        node
        for node in nodes
        if node.get("type") == "disk"
    ]

    if not drives:
        print("No block devices of type 'disk' found.")
        return

    print("Block-device reference map")
    print(f"Found {len(drives)} physical/virtual disks.")

    for drive in drives:
        print_drive(
            drive,
            nodes,
            by_links,
            mounts,
            fstab,
        )

    # ------------------------------------------------------------
    # Show non-disk mounts as well
    # ------------------------------------------------------------

    print()
    print("=" * 88)
    print("Other mounted sources (tmpfs, NFS, overlay, etc.):")

    disk_paths = {
        realpath(
            "/dev/" + (
                node.get("kname")
                or node.get("name")
            )
        )
        for node in nodes
    }

    for source, mountpoints in sorted(mounts.items()):
        if source not in disk_paths:
            print(
                f"  {source} -> "
                + ", ".join(mountpoints)
            )


if __name__ == "__main__":
    main()