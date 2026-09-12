#!/usr/bin/env python3
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def check_root():
    """Verify script is executed with root privileges."""
    if os.geteuid() != 0:
        sys.stderr.write("Error: Root privileges required. Run with sudo.\n")
        sys.exit(1)


def check_smartctl(dev_path: str):
    """Method 1: smartctl (-n standby prevents drive spin-up)."""
    if not shutil.which("smartctl"):
        return None

    try:
        res = subprocess.run(
            ["smartctl", "-i", "-n", "standby", dev_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if res.returncode == 0:
            return "SPINNING"
        elif res.returncode == 2:
            return "SPUN DOWN"
    except Exception:
        pass
    return None


def check_sdparm(dev_path: str):
    """Method 2: sdparm (SCSI parameter tool)."""
    if not shutil.which("sdparm"):
        return None

    try:
        res = subprocess.run(
            ["sdparm", "--command=sense", dev_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        out = res.stdout
        if re.search(r"standby|stopped|not ready|power condition", out, re.I):
            return "SPUN DOWN"
        elif re.search(r"No additional sense", out, re.I):
            return "SPINNING"
    except Exception:
        pass
    return None


def check_sg_requests(dev_path: str):
    """Method 3: sg_requests (sg3_utils SCSI pass-through)."""
    if not shutil.which("sg_requests"):
        return None

    try:
        res = subprocess.run(
            ["sg_requests", dev_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        out = res.stdout
        if re.search(r"power condition|initializing command|not ready", out, re.I):
            return "SPUN DOWN"
        elif re.search(r"No additional sense", out, re.I):
            return "SPINNING"
    except Exception:
        pass
    return None


def check_sysfs(disk_name: str):
    """Method 4: Kernel /sys runtime status."""
    sys_path = Path(f"/sys/block/{disk_name}/device/power/runtime_status")
    if sys_path.is_file():
        try:
            status = sys_path.read_text().strip()
            if status == "active":
                return "SPINNING"
            elif status == "suspended":
                return "SPUN DOWN"
        except Exception:
            pass
    return None


def main():
    check_root()

    # Print table header
    print(f"{'DEVICE':<12} {'MODEL':<28} {'STATUS':<15} {'METHOD':<12}")
    print(f"{'------':<12} {'-----':<28} {'------':<15} {'------':<12}")

    # Process all SCSI/SATA drives
    for dev_dir in sorted(glob.glob("/sys/block/sd*")):
        disk = os.path.basename(dev_dir)
        dev_path = f"/dev/{disk}"

        # Retrieve drive model from sysfs
        model_file = Path(dev_dir) / "device" / "model"
        if model_file.is_file():
            try:
                # Truncate multiple spaces similar to `tr -s ' ' | xargs`
                model = " ".join(model_file.read_text().split())
            except Exception:
                model = "Unknown Model"
        else:
            model = "Unknown Model"

        status = None
        method = "none"

        # Fallback chain
        status = check_smartctl(dev_path)
        if status:
            method = "smartctl"
        else:
            status = check_sdparm(dev_path)
            if status:
                method = "sdparm"
            else:
                status = check_sg_requests(dev_path)
                if status:
                    method = "sg3_utils"
                else:
                    status = check_sysfs(disk)
                    if status:
                        method = "sysfs"

        if not status:
            status = "UNKNOWN / ERR"
            method = "none"

        # Slice model to 27 characters max to match formatting
        print(f"{dev_path:<12} {model[:27]:<28} {status:<15} {method:<12}")


if __name__ == "__main__":
    main()