#!/usr/bin/env python3
"""
Unified Disk Information Tool
Combines non-invasive drive spin-state checking, SMART temperature monitoring,
lsblk topology mapping, partition usage (via df -hP), and filesystem information.
"""

import json
import os
import re
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


def check_root():
    """Verify root privileges required for SMART and low-level disk access."""
    if os.geteuid() != 0:
        sys.stderr.write("Error: Root privileges required. Re-run with sudo.\n")
        sys.exit(1)


def which(cmd: str) -> bool:
    """Custom executable finder in PATH to avoid using shutil."""
    path = os.getenv("PATH", "")
    for p in path.split(os.pathsep):
        full = os.path.join(p, cmd)
        if os.access(full, os.X_OK) and not os.path.isdir(full):
            return True
    return False


# ----------------------------------------------------------------------
# Non-invasive Spin State & Temperature Methods
# ----------------------------------------------------------------------

def get_spin_state(dev_path: str, disk_name: str) -> tuple[str, str]:
    """
    Check drive power state without waking it up using a fallback chain.
    Returns (status, method_used).
    """
    # Method 1: smartctl -n standby (non-invasive)
    if which("smartctl"):
        try:
            res = subprocess.run(
                ["smartctl", "-i", "-n", "standby", dev_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if res.returncode == 0:
                return "SPINNING", "smartctl"
            elif res.returncode == 2:
                return "SPUN DOWN", "smartctl"
        except Exception:
            pass

    # Method 2: sdparm
    if which("sdparm"):
        try:
            res = subprocess.run(
                ["sdparm", "--command=sense", dev_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            out = res.stdout
            if re.search(r"standby|stopped|not ready|power condition", out, re.I):
                return "SPUN DOWN", "sdparm"
            elif re.search(r"No additional sense", out, re.I):
                return "SPINNING", "sdparm"
        except Exception:
            pass

    # Method 3: sg_requests
    if which("sg_requests"):
        try:
            res = subprocess.run(
                ["sg_requests", dev_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            out = res.stdout
            if re.search(r"power condition|initializing command|not ready", out, re.I):
                return "SPUN DOWN", "sg_requests"
            elif re.search(r"No additional sense", out, re.I):
                return "SPINNING", "sg_requests"
        except Exception:
            pass

    # Method 4: Sysfs runtime status
    sys_path = Path(f"/sys/block/{disk_name}/device/power/runtime_status")
    if sys_path.is_file():
        try:
            status = sys_path.read_text().strip()
            if status == "active":
                return "SPINNING", "sysfs"
            elif status == "suspended":
                return "SPUN DOWN", "sysfs"
        except Exception:
            pass

    return "UNKNOWN", "none"


def get_drive_temperature(dev_path: str, spin_state: str) -> str:
    """
    Retrieves temperature ONLY if the drive is already active/spinning.
    Prevents spin-up of sleeping drives.
    """
    if spin_state != "SPINNING":
        return "N/A (Spun Down)"

    if not which("smartctl"):
        return "N/A (No smartctl)"

    try:
        res = subprocess.run(
            ["smartctl", "-A", "-n", "standby", dev_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if res.returncode != 0 and res.returncode != 2:
            return "N/A"

        output = res.stdout
        for line in output.split("\n"):
            if "Temperature_" not in line:
                continue

            line = re.sub(r'\s*\([^)]*\)$', '', line)
            line = line.strip().split(" ")
            return f"{line[-1]}°C"


        # Standard SATA SMART Temperature (Attributes 194 or 190)
        #match = re.search(r"^(194|190)\s+Temperature_\w+.*?\s+(\d+)\s*$", output, re.M)
        #if match:
        #    line = match.group(0).split()
        #    return f"{line[-1]}°C"

        # NVMe SMART log temperature format
        #nvme_match = re.search(r"Temperature:\s*(\d+)\s*Celsius", output, re.I)
        #if nvme_match:
        #    return f"{nvme_match.group(1)}°C"
        return "N/A (NVME)"

    except Exception:
        pass

    return "N/A"


# ----------------------------------------------------------------------
# Topology, Aliases, and Filesystem Mapping
# ----------------------------------------------------------------------

def realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def get_by_links() -> dict[str, list[str]]:
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


def get_mounts() -> dict[str, list[str]]:
    mounts = defaultdict(list)
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return mounts

    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            fields = left.split()
            post = right.split()

            mount_point = bytes(fields[4], "utf-8").decode("unicode_escape")
            source = post[1] if len(post) > 1 else "?"

            if source.startswith("/") and os.path.exists(source):
                key = realpath(source)
            else:
                key = source

            mounts[key].append(mount_point)
        except (ValueError, IndexError):
            continue
    return mounts


def unescape_fstab(value: str) -> str:
    return value.replace(r"\040", " ").replace(r"\011", "\t")


def get_fstab() -> dict[str, list[str]]:
    refs = defaultdict(list)
    try:
        lines = Path("/etc/fstab").read_text(encoding="utf-8").splitlines()
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


def get_lsblk_tree() -> list[dict]:
    try:
        raw = subprocess.check_output(
            ["lsblk", "-J", "-o", "NAME,KNAME,PKNAME,TYPE,SIZE,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINTS"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return json.loads(raw).get("blockdevices", [])
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        return []


def flatten_tree(nodes: list[dict], parent=None, out=None) -> list[dict]:
    if out is None:
        out = []
    for node in nodes:
        node["_parent"] = parent
        out.append(node)
        for child in node.get("children") or []:
            flatten_tree([child], node, out)
    return out


def get_df_info() -> dict[str, dict[str, str]]:
    """
    Runs `df -hP` to collect filesystem space information without triggering
    disk spin-ups on unmounted or idle drives.
    Returns mapping of device paths and mount points to formatted strings.
    """
    df_map = {}
    try:
        res = subprocess.run(
            ["df", "-hP"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if res.returncode == 0:
            lines = res.stdout.strip().splitlines()
            if len(lines) > 1:
                for line in lines[1:]:
                    parts = line.split(maxsplit=5)
                    if len(parts) >= 6:
                        dev, size, used, avail, pct, mp = parts
                        info = {
                            "used": used,
                            "size": size,
                            "pct": pct,
                            "display": f"{used} / {size} ({pct})",
                        }
                        if dev.startswith("/"):
                            df_map[realpath(dev)] = info
                        df_map[mp] = info
    except Exception:
        pass
    return df_map


def get_partition_capacity_str(dev_path: str, mountpoints: list[str], lsblk_size: str, df_info: dict) -> str:
    """Lookup usage in df_info; fallback to lsblk raw size if unmounted."""
    real_p = realpath(dev_path)
    if real_p in df_info:
        return df_info[real_p]["display"]

    for mp in mountpoints:
        if mp in df_info:
            return df_info[mp]["display"]

    if lsblk_size:
        return f"- / {lsblk_size}"
    return "- / -"


# ----------------------------------------------------------------------
# Main Formatting & Output
# ----------------------------------------------------------------------

def main():
    check_root()

    all_nodes = flatten_tree(get_lsblk_tree())
    by_links = get_by_links()
    mounts = get_mounts()
    fstab = get_fstab()
    df_info = get_df_info()

    drives = [node for node in all_nodes if node.get("type") == "disk"]
    if not drives:
        print("No physical/virtual disks found.")
        return

    # Table Header Definition
    divider = "=" * 148
    sub_divider = "-" * 148

    print(divider)
    print(
        f"{'DRIVE ID':<12} {'STATE':<11} {'TEMP':<8} {'DRIVE CAPACITY':<22} {'PARTITION':<12} {'FS TYPE':<10} {'PART CAPACITY':<24} {'ALIASES / FSTAB / MOUNT POINTS'}"
    )
    print(divider)

    for drive in drives:
        kname = drive.get("kname") or drive.get("name") or ""
        dev_path = f"/dev/{kname}"

        # 1. Spin State & Temperature (non-wake)
        spin_state, _ = get_spin_state(dev_path, kname)
        temp = get_drive_temperature(dev_path, spin_state)

        # 2. Drive Aliases
        drive_aliases = sorted(set(by_links.get(realpath(dev_path), [])))

        # 3. Find Drive Children / Partitions
        drive_nodes_set = {id(drive)}
        changed = True
        while changed:
            changed = False
            for node in all_nodes:
                parent = node.get("_parent")
                if parent is not None and id(parent) in drive_nodes_set and id(node) not in drive_nodes_set:
                    drive_nodes_set.add(id(node))
                    changed = True

        children = [n for n in all_nodes if id(n) in drive_nodes_set and n is not drive]

        # 4. Drive Total Capacity
        drive_total_size = drive.get("size") or "-"
        drive_cap_str = f"- / {drive_total_size}"

        partition_info_list = []

        if not children:
            # Handle drive without partitions
            node_path = realpath(dev_path)
            mp_list = mounts.get(node_path, [])
            p_cap_str = get_partition_capacity_str(dev_path, mp_list, drive_total_size, df_info)

            ref_lines = []
            for mp in mp_list:
                ref_lines.append(f"Mounted: {mp}")
            for f_mp in fstab.get(node_path, []):
                ref_lines.append(f"fstab: {f_mp}")
            for alias in drive_aliases:
                ref_lines.append(f"Alias: {alias}")

            partition_info_list.append({
                "part_id": "(no parts)",
                "fstype": drive.get("fstype") or "-",
                "cap_str": p_cap_str,
                "refs": ref_lines or ["-"],
            })
        else:
            for child in children:
                c_kname = child.get("kname") or child.get("name") or ""
                c_dev_path = f"/dev/{c_kname}"
                c_real_path = realpath(c_dev_path)
                c_size = child.get("size") or ""

                mp_list = mounts.get(c_real_path, [])
                p_cap_str = get_partition_capacity_str(c_dev_path, mp_list, c_size, df_info)

                ref_lines = []
                for mp in mp_list:
                    ref_lines.append(f"Mounted: {mp}")
                for f_mp in fstab.get(c_real_path, []):
                    ref_lines.append(f"fstab: {f_mp}")
                for alias in sorted(set(by_links.get(c_real_path, []))):
                    ref_lines.append(f"Alias: {alias}")

                partition_info_list.append({
                    "part_id": c_dev_path,
                    "fstype": child.get("fstype") or "-",
                    "cap_str": p_cap_str,
                    "refs": ref_lines or ["-"],
                })

        # Output rows for this drive
        first_row = True
        for part in partition_info_list:
            part_id = part["part_id"]
            fstype = part["fstype"]
            part_cap = part["cap_str"]
            refs = part["refs"]

            ref_first = refs[0] if refs else "-"

            if first_row:
                print(
                    f"{dev_path:<12} {spin_state:<11} {temp:<8} {drive_cap_str:<22} {part_id:<12} {fstype:<10} {part_cap:<24} {ref_first}"
                )
                first_row = False
            else:
                print(
                    f"{'':<12} {'':<11} {'':<8} {'':<22} {part_id:<12} {fstype:<10} {part_cap:<24} {ref_first}"
                )

            # Print additional reference/alias lines under the partition
            for extra_ref in refs[1:]:
                print(f"{'':<12} {'':<11} {'':<8} {'':<22} {'':<12} {'':<10} {'':<24} {extra_ref}")

        # Print drive-level aliases if present
        if drive_aliases:
            for d_alias in drive_aliases:
                print(f"{'':<12} {'':<11} {'':<8} {'':<22} Drive Alias: {d_alias}")

        print(sub_divider)


if __name__ == "__main__":
    main()