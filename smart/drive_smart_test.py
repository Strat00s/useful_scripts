#!/usr/bin/env python3
"""
SMART Self-Test Runner — launches short or extended SMART self-tests on every
drive in the system where the drive (or its controller) supports them, waits
for completion, and prints a formatted results report.

Behaviour notes:
  * Drives are discovered via `lsblk` (whole disks, including virtual ones)
    unioned with `smartctl --scan` (SMART-visible devices).
  * Each candidate is probed for self-test support. Virtual / QEMU / virtio /
    hardware-RAID passthrough disks that do not implement SMART self-tests
    are reported as SKIPPED, not as failures.
  * By default all supported drives are started at once (the launch commands
    are quick; the tests themselves run concurrently on the drives). Use
    --sequential to fully test one drive before starting the next.
  * Completion is detected from the self-test log (ATA/SCSI/NVMe variants),
    tracking in-progress entries and remaining percentages. A generous
    timeout derived from smartctl's own estimated polling times guards
    against drives that never update the log.
  * Ctrl-C aborts any started tests (smartctl -X) before exiting.

Requirements: root privileges, smartmontools (`smartctl`), `lsblk` (utillinux).
Exit status: 0 = all launched tests completed without error (skips allowed),
             1 = at least one failed / aborted / errored / timed out.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Terminal formatting (self-contained copy of the helpers used by
# drive_smart_status.py so that every script stays standalone)
# ---------------------------------------------------------------------------

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"


USE_COLOR = sys.stdout.isatty()


def colorize(text, color_code):
    if USE_COLOR:
        return f"{color_code}{text}{Colors.ENDC}"
    return text


def visible_len(text):
    return len(ANSI_ESCAPE.sub("", str(text)))


def fit(text, width):
    text = str(text)
    if visible_len(text) <= width:
        return text
    out = []
    vis = 0
    i = 0
    while i < len(text):
        m = ANSI_ESCAPE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        if vis >= width - 1:
            break
        out.append(text[i])
        vis += 1
        i += 1
    out.append("…")
    if ANSI_ESCAPE.search("".join(out)):
        out.append(Colors.ENDC)
    return "".join(out)


def print_table(headers, rows, max_widths=None):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], visible_len(value))
    if max_widths:
        widths = [min(widths[i], max_widths[i]) for i in range(len(headers))]

    print("│".join(fit(h, w).center(w) for h, w in zip(headers, widths)))
    print("├".join("─" * w for w in widths))
    for row in rows:
        cells = []
        for i, value in enumerate(row):
            pad = widths[i] - visible_len(value)
            cells.append(fit(value, widths[i]) + " " * max(0, pad))
        print("│".join(cells))


def human_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def run(cmd, timeout=120):
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def run_json(cmd, timeout=120):
    res = run(cmd, timeout=timeout)
    if res is None:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def check_prerequisites():
    if not shutil.which("smartctl"):
        print(colorize("Error: 'smartctl' is not installed or not in PATH.", Colors.FAIL))
        print("Install smartmontools first (apt install smartmontools / dnf install smartmontools).")
        sys.exit(2)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sys.stderr.write(colorize("Error: root privileges required to launch self-tests. Run with sudo.\n", Colors.FAIL))
        sys.exit(2)


# ---------------------------------------------------------------------------
# Drive discovery and capability probing
# ---------------------------------------------------------------------------

def discover_block_disks():
    """Whole-disk block devices from lsblk: {devpath: info dict}."""
    disks = {}
    if not shutil.which("lsblk"):
        return disks

    res = run([
        "lsblk", "-J", "-b",
        "-o", "NAME,KNAME,TYPE,SIZE,MODEL,SERIAL,TRAN,VENDOR",
    ])
    if res is None or res.returncode != 0:
        return disks

    try:
        tree = json.loads(res.stdout).get("blockdevices", [])
    except json.JSONDecodeError:
        return disks

    def walk(nodes):
        for node in nodes:
            if node.get("type") == "disk":
                kname = node.get("kname") or node.get("name")
                if kname:
                    disks["/dev/" + kname] = {
                        "model": (node.get("model") or "").strip(),
                        "serial": (node.get("serial") or "").strip(),
                        "size": node.get("size"),
                        "transport": node.get("tran"),
                        "vendor": node.get("vendor"),
                    }
            walk(node.get("children") or [])

    walk(tree)
    return disks


def discover_smartctl_devices():
    """Device names smartctl believes are SMART-accessible."""
    data = run_json(["smartctl", "--scan", "--json"])
    if not data:
        return []
    return [d["name"] for d in data.get("devices", []) if d.get("name")]


SMARTCTL_NO_DEVICE = "unable to open device"


def probe_device(dev):
    """
    Return capability info for one device.

    Keys: name, model, serial, protocol, smart_visible, self_test_supported,
          skip_reason, est_short_min, est_long_min
    """
    info = {
        "name": dev,
        "model": "",
        "serial": "",
        "protocol": "",
        "smart_visible": False,
        "self_test_supported": False,
        "skip_reason": None,
        "est_short_min": None,
        "est_long_min": None,
    }

    data = run_json(["smartctl", "-a", "--json", dev])
    if data is None:
        info["skip_reason"] = "smartctl returned no parseable data"
        return info

    if data.get("not_found") or data.get("unable_to_open_device"):
        info["skip_reason"] = SMARTCTL_NO_DEVICE
        return info

    info["smart_visible"] = True
    info["model"] = data.get("model_name") or ""
    info["serial"] = data.get("serial_number") or ""
    info["protocol"] = (data.get("device") or {}).get("protocol") or \
                       (data.get("device") or {}).get("type") or ""

    # --- self-test support, per protocol -----------------------------------
    if "nvme_smart_health_information_log" in data or \
            (data.get("device") or {}).get("type") == "nvme":
        # NVMe: self-tests run through the device self-test log. Assume
        # support; a failed launch is caught later.
        info["self_test_supported"] = True
    elif data.get("ata_smart_attributes") or info["protocol"].upper() == "SATA":
        info["self_test_supported"] = bool(
            (data.get("ata_smart_data") or {}).get("capabilities", {}).get("self_tests_supported", False)
        )
    else:
        # SCSI / SAS / unknown: smartctl states support in the selftest log.
        res = run(["smartctl", "-l", "selftest", dev])
        out = res.stdout if res else ""
        if re.search(r"does not support self test", out, re.I):
            info["self_test_supported"] = False
        elif re.search(r"self-test (routine|log)|background self-test", out, re.I):
            info["self_test_supported"] = True
        else:
            # Ambiguous (e.g. QEMU SCSI target answering the log request but
            # nothing real behind it). Try the launch; errors are handled.
            info["self_test_supported"] = True

    if not info["self_test_supported"]:
        info["skip_reason"] = "device does not implement SMART self-tests"
        return info

    # --- estimated durations from the capability page (ATA mainly) ---------
    res = run(["smartctl", "-c", dev])
    if res is not None:
        m = re.search(r"Short self-test routine recommended polling time:\s*\(\s*(\d+)\)", res.stdout)
        if m:
            info["est_short_min"] = int(m.group(1))
        m = re.search(r"Extended self-test routine recommended polling time:\s*\(\s*(\d+)\)", res.stdout)
        if m:
            info["est_long_min"] = int(m.group(1))

    return info


# ---------------------------------------------------------------------------
# Self-test log parsing (ATA / SCSI / NVMe JSON shapes)
# ---------------------------------------------------------------------------

def extract_entries(data):
    """Return (entries, nvme_current_activity) from a `-l selftest --json` doc."""
    entries = []

    ata = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table")
    if ata:
        entries = ata

    if not entries:
        entries = data.get("nvme_self_test_log", {}).get("table") or []

    if not entries:
        entries = data.get("self_test_log", {}).get("elements") or []

    activity = None
    nvme = data.get("nvme_self_test_log") or {}
    if nvme:
        cur = nvme.get("current_activity")
        if isinstance(cur, dict):
            activity = cur.get("value")
        elif isinstance(cur, int):
            activity = cur

    # Keep only usable dict entries, newest first (smartctl lists newest 0).
    return [e for e in entries if isinstance(e, dict)], activity


def entry_fields(entry):
    """Normalize (status_string, passed, remaining_percent) from one entry."""
    status = entry.get("status")
    remaining = None

    if isinstance(status, dict):
        status_str = status.get("string", "")
        passed = status.get("passed")
        remaining = status.get("remaining_percent")
    else:
        status_str = entry.get("status_string") or entry.get("desc") or ""
        passed = entry.get("passed")

    # Fallback just in case some NVMe controllers place it at the root
    if remaining is None:
        remaining = entry.get("remaining_percent")

    return str(status_str), passed, remaining


def classify(status_str, passed):
    lower = status_str.lower()
    if "in progress" in lower:
        return "RUNNING"
    if "aborted" in lower or "interrupted" in lower:
        return "ABORTED"
    if passed is False:
        return "FAILED"
    if "without error" in lower or "completed without" in lower:
        return "PASSED"
    if "failed" in lower or "fatal" in lower or "error" in lower:
        return "FAILED"
    if passed is True:
        return "PASSED"
    return "UNKNOWN"


def read_selftest(dev):
    """
    Return (latest_entry_or_None, nvme_activity, raw) for a device.
    latest_entry is None when the drive has no log entries yet.
    """
    # 1. Add '-c' to the command to fetch the real-time execution capabilities
    data = run_json(["smartctl", "-c", "-l", "selftest", "--json", dev])
    if data is None:
        return None, None, None

    entries, activity = extract_entries(data)

    # 2. Extract the live execution status (which bypasses the buggy log table)
    exec_status = data.get("ata_smart_data", {}).get("self_test", {}).get("status", {})
    status_str = exec_status.get("string", "")

    # 3. If there is a valid execution status, prepend it as the most authoritative entry
    if status_str and "was never started" not in status_str.lower():
        entries.insert(0, {"status": exec_status})

    return (entries[0] if entries else None), activity, data


def entry_signature(entry):
    """Identity of a log entry, to detect that a *new* test has been logged."""
    if entry is None:
        return None
    return json.dumps(
        {
            "num": entry.get("num"),
            "date": entry.get("date_time"),
            "hour": entry.get("lifetime_hours", entry.get("power_on_hours")),
            "status": str(entry.get("status")),
            "lba": entry.get("lba_of_next_test"),
        },
        sort_keys=True,
        default=str,
    )


# ---------------------------------------------------------------------------
# Test lifecycle
# ---------------------------------------------------------------------------

SMARTCTL_NAME = {"short": "short", "long": "long"}
SMARTCTL_LABEL = {"short": "Short", "long": "Extended"}
FALLBACK_ESTIMATE_MIN = {"short": 30, "long": 720}


def launch_test(dev, test):
    """
    Start one self-test. Returns (state, message):
      started / attached (join a test already running) / skipped / error
    """
    res = run(["smartctl", "-d","auto", "-t", SMARTCTL_NAME[test], dev])
    if res is None:
        return "error", "smartctl launch failed to run"

    out = res.stdout.strip()
    if res.returncode == 0:
        return "started", ""

    lower = out.lower()
    if re.search(r"not supported|unsupported|does not support", lower):
        return "skipped", first_useful_line(out) or "self-tests not supported"
    if re.search(r"already in progress|self-test.*in progress", lower):
        return "attached", "joining self-test already in progress"
    return "error", first_useful_line(out) or f"smartctl exit {res.returncode}"


def first_useful_line(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line and not line.startswith(("smartctl ", "Copyright", "=== ")):
            return line
    return ""


class DriveTest:
    """State machine for one drive's in-flight (or finished) self-test."""

    def __init__(self, info, test):
        self.info = info
        self.dev = info["name"]
        self.test = test
        self.state = "queued"          # queued|running|done|skipped|error|timeout
        self.result = None             # PASSED/FAILED/ABORTED/UNKNOWN
        self.message = ""
        self.progress = None           # 0..100 while running
        self.start_wall = None
        self.finish_wall = None
        self.seen_running = False
        self.baseline_sig = None
        self.estimate_s = None

    # -- timing ------------------------------------------------------------
    @property
    def elapsed(self):
        if self.start_wall is None:
            return 0.0
        end = self.finish_wall or time.monotonic()
        return end - self.start_wall

    def timeout_seconds(self):
        est_min = None
        if self.test == "short":
            est_min = self.info.get("est_short_min")
        else:
            est_min = self.info.get("est_long_min")
        if not est_min:
            est_min = FALLBACK_ESTIMATE_MIN[self.test]
        self.estimate_s = est_min * 60
        return self.estimate_s * 1.75 + 180

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        state, msg = launch_test(self.dev, self.test)
        self.message = msg
        if state in ("started", "attached"):
            self.state = "running"
            self.start_wall = time.monotonic()
            if state == "started":
                # Only a fresh launch needs a baseline: an entry identical to
                # it is "not ours yet". Joined tests are ours by definition.
                entry, _, _ = read_selftest(self.dev)
                self.baseline_sig = entry_signature(entry)
        elif state == "skipped":
            self.state = "skipped"
        else:
            self.state = "error"

    def poll(self):
        if self.state != "running":
            return

        entry, activity, raw = read_selftest(self.dev)
        if raw is None:
            return  # transient failure, retry next cycle

        nvme_running = activity in (1, 2)
        sig = entry_signature(entry)

        if entry is not None:
            status_str, passed, remaining = entry_fields(entry)
            state = classify(status_str, passed)

            if state == "RUNNING" or nvme_running:
                self.seen_running = True
                self.progress = None if remaining is None else max(0, min(100, 100 - int(remaining)))
                return

            # Entry is no longer in progress. Is it *our* test?
            ours = (
                self.seen_running
                or (self.baseline_sig is None)
                or (sig != self.baseline_sig)
            )
            if ours:
                self._finish(classify(status_str, passed), status_str)
                return

        elif nvme_running:
            self.seen_running = True
            return

        # Nothing conclusive yet. If the drive never shows in-progress
        # entries and its estimate has elapsed, trust a type-matching entry.
        if self.elapsed > max(self.timeout_seconds() * 0.4, 120) and entry is not None:
            status_str, passed, _ = entry_fields(entry)
            self._finish(classify(status_str, passed), status_str + " (log not updated)")

        if self.elapsed > self.timeout_seconds():
            self.state = "timeout"
            self.finish_wall = time.monotonic()
            self.message = (
                "no completion recorded — test may still be running "
                f"(abort with: smartctl -X {self.dev})"
            )

    def _finish(self, result, detail):
        self.state = "done"
        self.result = result
        self.finish_wall = time.monotonic()
        self.progress = 100
        self.message = detail


def abort_all(tests):
    running = [t for t in tests if t.state == "running"]
    if not running:
        return
    print()
    print(colorize("Interrupt received — aborting in-progress self-tests...", Colors.WARNING))
    for t in running:
        run(["smartctl", "-X", t.dev])
        t.state = "error"
        t.message = "aborted by user (smartctl -X sent)"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

RESULT_STYLE = {
    "PASSED": Colors.OKGREEN + Colors.BOLD,
    "FAILED": Colors.FAIL + Colors.BOLD,
    "ABORTED": Colors.WARNING,
    "UNKNOWN": Colors.WARNING,
    "RUNNING": Colors.OKCYAN,
}


def style_result(text):
    style = RESULT_STYLE.get(text)
    return colorize(text, style) if style else text


def print_plan(tests, test_name):
    print()
    print(colorize("PLAN", Colors.HEADER + Colors.BOLD))
    rows = []
    for t in tests:
        est = ""
        if t.info.get("est_short_min") or t.info.get("est_long_min"):
            minutes = t.info["est_short_min"] if test_name == "short" else t.info["est_long_min"]
            est = f"~{minutes}m" if minutes else "?"
        if t.state == "queued":
            status = colorize("ready", Colors.OKBLUE)
            detail = ""
        elif t.state == "skipped":
            status = colorize("SKIP", Colors.WARNING)
            detail = t.message
        else:
            status = colorize("ERROR", Colors.FAIL)
            detail = t.message
        rows.append([
            t.dev,
            fit(t.info.get("model") or "-", 24),
            t.info.get("protocol") or "-",
            SMARTCTL_LABEL[test_name],
            est,
            status,
            detail,
        ])
    print_table(
        ["DEVICE", "MODEL", "PROTO", "TEST", "EST", "STATE", "DETAIL"],
        rows,
        max_widths=[12, 24, 7, 9, 5, 6, 52],
    )


def progress_line(t):
    bar = ""
    if t.progress is not None:
        filled = int(t.progress / 10)
        bar = f"[{'#' * filled}{'.' * (10 - filled)}] {t.progress:3d}%"
    else:
        bar = "[..........]  ??%"
    est = f" / ~{human_duration(t.estimate_s)}" if t.estimate_s else ""
    return (
        f"{t.dev:<10} {SMARTCTL_LABEL[t.test]:<8} {bar} "
        f"elapsed {human_duration(t.elapsed)}{est}"
    )


_PROGRESS_ACTIVE = [False]


def print_progress(tests, is_tty):
    active = [t for t in tests if t.state == "running"]
    if is_tty:
        if not active:
            clear_progress()
            return
        width = shutil.get_terminal_size().columns or 80
        parts = []
        for t in active[:4]:
            pct = "??" if t.progress is None else f"{t.progress:3d}%"
            bar = "?" if t.progress is None else "#" * (t.progress // 10)
            parts.append(
                f"{t.dev} [{bar:<10}] {pct} {human_duration(t.elapsed)}"
            )
        if len(active) > 4:
            parts.append(f"+{len(active) - 4} more")
        sys.stdout.write("\r\x1b[K" + colorize(fit("  ".join(parts), width - 1), Colors.DIM))
        sys.stdout.flush()
        _PROGRESS_ACTIVE[0] = True
    else:
        stamp = time.strftime("%H:%M:%S")
        for t in active:
            print(f"[{stamp}] {progress_line(t)}")


def clear_progress():
    if _PROGRESS_ACTIVE[0]:
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()
        _PROGRESS_ACTIVE[0] = False


def print_results(tests, test_name):
    print()
    print(colorize("RESULTS", Colors.HEADER + Colors.BOLD))
    rows = []
    for t in tests:
        if t.state == "done":
            verdict = style_result(t.result)
        elif t.state == "skipped":
            verdict = colorize("SKIPPED", Colors.WARNING)
        elif t.state == "timeout":
            verdict = colorize("TIMED OUT", Colors.FAIL + Colors.BOLD)
        elif t.state == "error":
            verdict = colorize("ERROR", Colors.FAIL + Colors.BOLD)
        else:
            verdict = colorize("NOT RUN", Colors.WARNING)
        elapsed = human_duration(t.elapsed) if t.start_wall else "-"
        rows.append([
            t.dev,
            fit(t.info.get("model") or "-", 24),
            t.info.get("protocol") or "-",
            SMARTCTL_LABEL[test_name],
            verdict,
            elapsed,
            fit(t.message, 52),
        ])
    print_table(
        ["DEVICE", "MODEL", "PROTO", "TEST", "RESULT", "ELAPSED", "DETAIL"],
        rows,
        max_widths=[12, 24, 7, 9, 9, 8, 52],
    )

    # Post-test SMART health snapshot for drives that were actually tested.
    tested = [t for t in tests if t.state == "done"]
    if tested:
        print()
        print(colorize("POST-TEST SMART HEALTH", Colors.HEADER + Colors.BOLD))
        rows = []
        for t in tested:
            data = run_json(["smartctl", "-a", "--json", t.dev])
            if data is None:
                rows.append([t.dev, "?", "?", "-"])
                continue
            health = data.get("smart_status", {}).get("passed")
            health_str = (
                style_result("PASSED") if health
                else colorize("FAILED", Colors.FAIL + Colors.BOLD) if health is False
                else colorize("UNKNOWN", Colors.WARNING)
            )
            fails = 0
            table = (data.get("ata_smart_self_test_log", {})
                     .get("standard", {}).get("table", []))
            for entry in table:
                st, passed, _ = entry_fields(entry)
                if classify(st, passed) == "FAILED":
                    fails += 1
            rows.append([t.dev, health_str, str(fails),
                         f"power-on {data.get('power_on_time', {}).get('hours', '?')}h"])
        print_table(["DEVICE", "SMART STATUS", "LOGGED TEST FAILURES", "LIFETIME"], rows,
                    max_widths=[12, 12, 21, 20])


def summarize(tests):
    counts = {}
    for t in tests:
        key = t.result if t.state == "done" else t.state.upper()
        counts[key] = counts.get(key, 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    print()
    bad = [t for t in tests
           if (t.state == "done" and t.result != "PASSED")
           or t.state in ("error", "timeout")]
    if bad:
        print(colorize(f"Summary : {summary}", Colors.FAIL + Colors.BOLD))
        return 1
    print(colorize(f"Summary : {summary}", Colors.OKGREEN + Colors.BOLD))
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="drive_smart_test.py",
        description="Run short or extended SMART self-tests on all drives "
                    "that support them, wait for completion, and report.",
    )
    p.add_argument(
        "-t", "--test", required=True, choices=["short", "long", "extended"],
        help="self-test type: 'short' (few minutes) or 'long'/'extended' (hours)",
    )
    p.add_argument(
        "-d", "--drives", nargs="+", action="extend", metavar="DEV",
        default=None,
        help="restrict to these devices (repeatable; default: every disk found)",
    )
    p.add_argument(
        "-p", "--poll", type=int, default=15, metavar="SECONDS",
        help="poll interval while tests run (default: 15)",
    )
    p.add_argument(
        "-s", "--sequential", action="store_true",
        help="test one drive at a time instead of all drives concurrently",
    )
    return p


def collect_targets(args):
    disks = discover_block_disks()
    scan = discover_smartctl_devices()

    candidates = list(dict.fromkeys(list(disks.keys()) + scan))
    if args.drives:
        wanted = {os.path.realpath(d) for d in args.drives}
        candidates = [c for c in candidates if os.path.realpath(c) in wanted]
        unknown = wanted - {os.path.realpath(c) for c in candidates}
        for u in sorted(unknown):
            print(colorize(f"Warning: requested device not found: {u}", Colors.WARNING))

    tests = []
    for dev in candidates:
        disk = disks.get(dev, {})
        if dev not in scan and not args.drives:
            # Visible to the kernel but not to smartctl: virtual / passthrough.
            t = DriveTest({"name": dev, "model": disk.get("model", ""),
                           "protocol": disk.get("transport") or "virt",
                           "est_short_min": None, "est_long_min": None}, args.test)
            t.state = "skipped"
            t.message = "not SMART-accessible (virtual/qemu or controller hidden)"
            tests.append(t)
            continue

        info = probe_device(dev)
        t = DriveTest(info, args.test)
        if not info["self_test_supported"] or info["skip_reason"]:
            t.state = "skipped"
            t.message = info["skip_reason"] or "self-tests not supported"
        tests.append(t)
    return tests


def main():
    args = build_parser().parse_args()
    test_name = "long" if args.test in ("long", "extended") else "short"
    args.test = test_name  # normalize: "extended" -> smartctl "long"

    check_prerequisites()

    if not shutil.which("lsblk"):
        print(colorize("Warning: 'lsblk' unavailable; relying on smartctl scan only.", Colors.WARNING))

    print(colorize(f"Discovering drives (test: {SMARTCTL_LABEL[test_name]})...", Colors.OKBLUE))
    tests = collect_targets(args)
    if not tests:
        print(colorize("No drives found on this system.", Colors.WARNING))
        return 0

    launchable = [t for t in tests if t.state == "queued"]
    print_plan(tests, test_name)

    if not launchable:
        print("\nNothing to run — no drive on this system supports SMART self-tests.")
        print_results(tests, test_name)
        return summarize(tests)

    is_tty = sys.stdout.isatty()
    try:
        if args.sequential:
            for t in launchable:
                print(colorize(f"Testing {t.dev} ...", Colors.OKBLUE))
                t.start()
                while t.state == "running":
                    t.poll()
                    if t.state == "running":
                        print_progress(tests, is_tty)
                        time.sleep(max(1, args.poll))
                clear_progress()
                verdict = t.result or t.state
                print(f"{t.dev}: {verdict}" + (f" — {t.message}" if t.message else ""))
        else:
            for t in launchable:
                t.start()
            while any(t.state == "running" for t in launchable):
                for t in launchable:
                    t.poll()
                if any(t.state == "running" for t in launchable):
                    print_progress(tests, is_tty)
                    time.sleep(max(1, args.poll))
            clear_progress()
    except KeyboardInterrupt:
        clear_progress()
        abort_all(launchable)

    print_results(tests, test_name)
    return summarize(tests)


if __name__ == "__main__":
    raise SystemExit(main())
