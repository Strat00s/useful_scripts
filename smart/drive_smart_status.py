#!/usr/bin/env python3
"""
System SMART Drive Health Checker & Diagnostic Table Generator
Author: SMART Storage Monitoring (WD, Seagate, NVMe, SATA)
Requirements: smartmontools (`smartctl` must be installed)
"""

import json
import os
import re
import shutil
import subprocess
import sys

# Regex to strip ANSI escape codes when calculating visible string widths
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def visible_len(text):
    """Return the visible length of a string excluding ANSI escape sequences."""
    return len(ANSI_ESCAPE.sub("", str(text)))


def format_cell(text, width, align="left"):
    """Pad or truncate a string to fit a target visual column width."""
    vlen = visible_len(text)
    if vlen > width:
        plain = ANSI_ESCAPE.sub("", str(text))
        return plain[:width]
    padding = " " * (width - vlen)
    if align == "right":
        return padding + str(text)
    elif align == "center":
        left = " " * ((width - vlen) // 2)
        right = " " * (width - vlen - len(left))
        return left + str(text) + right
    return str(text) + padding


# ANSI Color Codes
class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"


# Color support detection
USE_COLOR = sys.stdout.isatty() or os.name != "nt"


def colorize(text, color_code):
    if USE_COLOR:
        return f"{color_code}{text}{Colors.ENDC}"
    return text


def check_prerequisites():
    """Verify that smartctl is installed and warn if not running as admin/root."""
    if not shutil.which("smartctl"):
        print(colorize("Error: 'smartctl' is not installed or not in system PATH.", Colors.FAIL))
        print("Please install smartmontools to use this script:")
        print("  - Debian/Ubuntu: sudo apt install smartmontools")
        print("  - RHEL/Fedora:   sudo dnf install smartmontools")
        print("  - macOS:         brew install smartmontools")
        print("  - Windows:       winget install smartmontools")
        sys.exit(1)

    is_admin = False
    if hasattr(os, "geteuid"):
        is_admin = os.geteuid() == 0
    elif os.name == "nt":
        import ctypes

        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0

    if not is_admin:
        print(colorize("[!] WARNING: Script is not running with administrative/root privileges.", Colors.WARNING))
        print("    smartctl may fail to access physical drive metrics or return incomplete data.")
        print("    Recommended: Run with 'sudo python3 check_drives.py'\n")


def get_available_drives():
    """Discover all drives on the system using smartctl scan."""
    try:
        res = subprocess.run(
            ["smartctl", "--scan", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        devices = data.get("devices", [])
        return [dev["name"] for dev in devices if "name" in dev]
    except Exception as e:
        print(colorize(f"Failed to scan drives: {e}", Colors.FAIL))
        return []


def parse_self_tests(data):
    """Extract SMART self-test history (counts, failures, last test details)."""
    test_table = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
    total_logged_count = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("count")

    if not test_table:
        test_table = data.get("nvme_self_test_log", {}).get("table", [])

    if not test_table:
        return {
            "total_count": 0,
            "short_count": 0,
            "long_count": 0,
            "failed_count": 0,
            "failed_details": [],
            "last_test": "No tests recorded",
        }

    short_cnt = 0
    long_cnt = 0
    failed_cnt = 0
    failed_details = []

    for entry in test_table:
        test_type_str = entry.get("type", {}).get("string", "").lower()
        if "short" in test_type_str:
            short_cnt += 1
        elif "extended" in test_type_str or "long" in test_type_str:
            long_cnt += 1

        status_info = entry.get("status", {})
        status_str = status_info.get("string", "")
        status_lower = status_str.lower()
        passed = status_info.get("passed")

        is_failure = False
        if passed is False:
            if not ("aborted" in status_lower or "interrupted" in status_lower or "in progress" in status_lower):
                is_failure = True
        elif any(kw in status_lower for kw in ["failure", "fatal", "damaged", "error"]) and "without error" not in status_lower:
            is_failure = True

        if is_failure:
            failed_cnt += 1
            t_type = entry.get("type", {}).get("string", "Unknown")
            t_hours = entry.get("lifetime_hours")
            hours_str = f" @ {t_hours:,}h" if t_hours is not None else ""
            failed_details.append(f"{t_type}: {status_str}{hours_str}")

    total_cnt = total_logged_count if total_logged_count is not None else len(test_table)

    latest = test_table[0]
    t_type = latest.get("type", {}).get("string", "Unknown")
    t_status = latest.get("status", {}).get("string", "Unknown")
    t_hours = latest.get("lifetime_hours")
    hours_str = f" @ {t_hours:,}h" if t_hours is not None else ""
    last_test_str = f"{t_type} ({t_status}{hours_str})"

    return {
        "total_count": total_cnt,
        "short_count": short_cnt,
        "long_count": long_cnt,
        "failed_count": failed_cnt,
        "failed_details": failed_details,
        "last_test": last_test_str,
    }


def inspect_drive(dev_name):
    """Retrieve and process SMART health metrics for a given device."""
    cmd = ["smartctl", "-a", "--json", dev_name]
    res = subprocess.run(cmd, capture_output=True, text=True)

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None

    model = data.get("model_name") or data.get("device", {}).get("model_name", "Unknown Model")
    serial = data.get("serial_number", "Unknown Serial")
    dev_type = data.get("device", {}).get("type", "unknown")

    smart_status = data.get("smart_status", {})
    smart_passed = smart_status.get("passed")

    temp = data.get("temperature", {}).get("current")
    power_hours = data.get("power_on_time", {}).get("hours")

    self_test_data = parse_self_tests(data)

    warnings = []
    stats = []

    if self_test_data["failed_count"] > 0:
        warnings.append(f"{self_test_data['failed_count']} Self-Test Failure(s)")

    ata_attrs = data.get("ata_smart_attributes", {}).get("table", [])
    attr_dict = {attr.get("id"): attr for attr in ata_attrs if "id" in attr}

    critical_ata_map = {
        5: "Reallocated Sectors",
        187: "Reported Uncorrectable",
        197: "Current Pending Sectors",
        198: "Offline Uncorrectable",
    }

    found_ata_issues = False
    for attr_id, label in critical_ata_map.items():
        if attr_id in attr_dict:
            raw_val = attr_dict[attr_id].get("raw", {}).get("value", 0)
            if raw_val > 0:
                warnings.append(f"{label}: {raw_val}")
                found_ata_issues = True

    if ata_attrs and not found_ata_issues:
        stats.append("Sectors Clean (0 bad/pending)")

    nvme_log = data.get("nvme_smart_health_information_log", {})
    if nvme_log:
        crit_warn = nvme_log.get("critical_warning", 0)
        pct_used = nvme_log.get("percentage_used")
        media_errors = nvme_log.get("media_errors", 0)

        if crit_warn != 0:
            warnings.append(f"NVMe Critical Warning: {crit_warn}")
        if media_errors > 0:
            warnings.append(f"Media Errors: {media_errors}")
        if pct_used is not None:
            stats.append(f"NVMe Wear: {pct_used}% Life Used")

    return {
        "device": dev_name,
        "model": model,
        "serial": serial,
        "type": dev_type,
        "passed": smart_passed,
        "temp": temp,
        "hours": power_hours,
        "self_test": self_test_data,
        "warnings": warnings,
        "stats": stats,
    }


def print_summary_table(drives_info):
    """Print a clean summary table of all detected drives."""
    cols = [
        ("DEVICE", 10),
        ("MODEL", 22),
        ("STATUS", 9),
        ("TEMP", 7),
        ("POWER-ON", 17),
        ("ALERTS", 9),
    ]
    widths = [w for _, w in cols]

    top_divider = "┌" + "┬".join("─" * w for w in widths) + "┐"
    top_divider = "├" + top_divider[1:]
    top_divider = top_divider[:-1] + "┤"
    mid_divider = "├" + "┼".join("─" * w for w in widths) + "┤"
    bot_divider = "└" + "┴".join("─" * w for w in widths) + "┘"

    # Header Box
    print("\n" + colorize("┌" + "─" * 79 + "┐", Colors.BOLD))
    title = "SYSTEM DRIVE HEALTH OVERVIEW"
    title_cell = format_cell(title, 79, align="center")
    print(colorize("│", Colors.BOLD) + colorize(title_cell, Colors.HEADER + Colors.BOLD) + colorize("│", Colors.BOLD))
    print(colorize(top_divider, Colors.BOLD))

    # Headers
    header_cells = [format_cell(f" {name}", w) for name, w in cols]
    header_row = "│" + "│".join(header_cells) + "│"
    print(colorize(header_row, Colors.BOLD))
    print(colorize(mid_divider, Colors.BOLD))

    for d in drives_info:
        dev_str = format_cell(f" {d['device']}", 10)
        mdl_str = format_cell(f" {d['model']}", 22)

        # Status
        if d["passed"] is True:
            stat_val = colorize("PASSED", Colors.OKGREEN + Colors.BOLD)
        elif d["passed"] is False:
            stat_val = colorize("FAILED!", Colors.FAIL + Colors.BOLD)
        else:
            stat_val = colorize("UNKNOWN", Colors.WARNING)
        stat_str = format_cell(f" {stat_val}", 9)

        # Temp
        if d["temp"] is not None:
            t_val = d["temp"]
            t_text = f"{t_val}°C"
            if t_val > 55:
                temp_val = colorize(t_text, Colors.FAIL + Colors.BOLD)
            elif t_val > 45:
                temp_val = colorize(t_text, Colors.WARNING)
            else:
                temp_val = colorize(t_text, Colors.OKGREEN)
        else:
            temp_val = "N/A"
        temp_str = format_cell(f" {temp_val}", 7)

        # Hours
        if d["hours"] is not None:
            hrs_val = d["hours"]
            days_val = round(hrs_val / 24)
            hrs_text = f"{hrs_val:,}h ({days_val}d)"
        else:
            hrs_text = "N/A"
        hrs_str = format_cell(f" {hrs_text}", 17)

        # Alerts
        if d["warnings"]:
            alrt_val = colorize(f"{len(d['warnings'])} Alert(s)", Colors.FAIL + Colors.BOLD)
        else:
            alrt_val = colorize("Clean", Colors.OKGREEN)
        alrt_str = format_cell(f" {alrt_val}", 9)

        print(f"│{dev_str}│{mdl_str}│{stat_str}│{temp_str}│{hrs_str}│{alrt_str}│")

    print(colorize(bot_divider, Colors.BOLD))


def format_card_line(content, width=80):
    """Format a line inside a diagnostic card with vertical side borders."""
    usable = width - 5
    vlen = visible_len(content)
    if vlen > usable:
        plain = ANSI_ESCAPE.sub("", str(content))
        content = plain[:usable]
        vlen = len(content)
    padding = " " * (usable - vlen)
    return f"│  {content}{padding} │"


def format_field(label, value, label_width=18):
    """Format label-value pairs with consistent label padding."""
    vlen = len(label)
    pad_len = max(0, label_width - vlen)
    lbl_str = colorize(label, Colors.BOLD) + (" " * pad_len)
    return f"{lbl_str} : {value}"


def print_detailed_cards(drives_info):
    """Print detailed diagnostic breakdown for each drive."""
    print("\n" + colorize("═" * 80, Colors.BOLD))
    title = "DETAILED DRIVE DIAGNOSTICS & TEST HISTORY"
    print(colorize(f"{title:^80}", Colors.HEADER + Colors.BOLD))
    print(colorize("═" * 80, Colors.BOLD))

    for d in drives_info:
        dev_title = f" DRIVE: {d['device']} ({d['model']}) "
        dashes = "─" * max(0, 80 - 3 - len(dev_title))
        print(f"\n┌─{colorize(dev_title, Colors.OKCYAN + Colors.BOLD)}{dashes}┐")

        # Basic Info
        print(format_card_line(format_field("Serial Number", d["serial"])))
        print(format_card_line(format_field("Interface/Type", d["type"])))

        # Status
        if d["passed"] is True:
            st_str = colorize("PASSED [HEALTHY]", Colors.OKGREEN + Colors.BOLD)
        elif d["passed"] is False:
            st_str = colorize("FAILED [CRITICAL HARDWARE FAULT]", Colors.FAIL + Colors.BOLD)
        else:
            st_str = colorize("UNKNOWN / UNREPORTED", Colors.WARNING)
        print(format_card_line(format_field("SMART Assessment", st_str)))

        # Power On & Temp
        if d["hours"] is not None:
            days = round(d["hours"] / 24, 1)
            print(format_card_line(format_field("Power-On Lifetime", f"{d['hours']:,} hours (~{days} days)")))

        if d["temp"] is not None:
            print(format_card_line(format_field("Temperature", f"{d['temp']} °C")))

        # Self-Test Summary
        st = d["self_test"]
        print("├" + "─" * 78 + "┤")
        print(format_card_line(format_field("SMART Self-Tests", f"{st['total_count']} lifetime test(s) logged")))
        print(
            format_card_line(
                format_field(
                    "Log Breakdown",
                    f"{st['short_count']} Short test(s) | {st['long_count']} Extended/Long test(s)",
                )
            )
        )
        print(format_card_line(format_field("Last Test Status", st["last_test"])))

        if st["failed_count"] > 0:
            fail_head = colorize(f"FAILED TESTS ({st['failed_count']} recorded):", Colors.FAIL + Colors.BOLD)
            print(format_card_line(format_field("Test Failure Log", fail_head)))
            for fail_item in st["failed_details"]:
                print(format_card_line(f"    - {colorize(fail_item, Colors.FAIL)}"))
        else:
            clean_msg = colorize("Clean (No test failures on record)", Colors.OKGREEN)
            print(format_card_line(format_field("Test Failure Log", clean_msg)))

        # Sector / NVMe Info
        print("├" + "─" * 78 + "┤")
        if d["stats"]:
            for stat_item in d["stats"]:
                print(format_card_line(format_field("Health Indicator", stat_item)))

        # Warnings / Alerts
        if d["warnings"]:
            crit_head = colorize("CRITICAL ALERTS & WARNINGS:", Colors.FAIL + Colors.BOLD)
            print(format_card_line(crit_head))
            for w in d["warnings"]:
                print(format_card_line(f"  [!] {colorize(w, Colors.FAIL + Colors.BOLD)}"))
        else:
            clean_al = colorize("No critical sector or controller alerts", Colors.OKGREEN)
            print(format_card_line(format_field("Alert Status", clean_al)))

        print("└" + "─" * 78 + "┘")


def main():
    check_prerequisites()
    drive_names = get_available_drives()

    if not drive_names:
        print(colorize("No drives detected during smartctl scan.", Colors.WARNING))
        sys.exit(0)

    drives_info = []
    for dev in drive_names:
        info = inspect_drive(dev)
        if info:
            drives_info.append(info)

    if not drives_info:
        print(colorize("Unable to parse SMART data for available drives.", Colors.FAIL))
        sys.exit(0)

    print_summary_table(drives_info)
    print_detailed_cards(drives_info)


if __name__ == "__main__":
    main()