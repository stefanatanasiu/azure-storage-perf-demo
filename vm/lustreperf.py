#!/usr/bin/env python3
"""lustreperf - Azure Managed Lustre performance, measured with the tests of the blob storage demo.

Runs the tests of blobperf (Premium vs Standard block blob storage) against an Azure Managed Lustre
(AMLFS) file system mounted on the VM, plus a large-file bandwidth test:

  latency     Read random files from a population of 1,000,000 64 KiB files, one file at a time with a cold
              client cache, in two passes over different files: 10,000 whole-file reads timed to the open
              (a round trip to the metadata server) and to the last byte, then 10,000 reads of the first 4 KiB
              only, timed to the first byte.
  throughput  Copy 100,000 64 KiB files from a RAM disk to the file system (write) and back (read), list
              them (names only, and with attributes like 'ls -l'), then delete them.
  bandwidth   Write and read large files with parallel sequential streams.

Needs the Lustre client and blobperf.py, which the base deployment installs in the same folder:
lustreperf shares its helpers (statistics, charts, and the results container that combines the results
of several VMs). Results and reports use their own lustre-* file names. To try the tool without a file
system, set LUSTREPERF_TEST_DIR=<directory>: the tests then run on that directory.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import ctypes
import datetime as dt
import errno
import gc
import glob
import hashlib
import html
import json
import math
import multiprocessing
import os
import platform
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blobperf as bp  # noqa: E402 - shared helpers, installed next to this file

VERSION = "1.0.0"
ENV_FILE = "/etc/blobperf/lustre.env"
KIB, MIB, GIB = 1024, 1024 ** 2, 1024 ** 3
DATASET_DIR = "latency"
MARKER_DIR = "_lustreperf"
KINDS = ("latency", "throughput", "bandwidth")
RESULT_NAME = re.compile(r"lustre-(latency|throughput|bandwidth)-\d{8}t\d{6}z\.json")
LATENCY_KEYS = ("avg", "p50", "p90", "p95", "p99", "p99_9", "max")
FILE_OPS = (("write", "Write (copy + sync)"), ("read", "Read"), ("list", "List names"),
            ("list_attributes", "List with attributes"), ("delete", "Delete"))
TUNABLES = (("mdc max_rpcs_in_flight", "mdc.*.max_rpcs_in_flight"),
            ("mdc max_mod_rpcs_in_flight", "mdc.*.max_mod_rpcs_in_flight"),
            ("osc max_rpcs_in_flight", "osc.*.max_rpcs_in_flight"),
            ("osc max_pages_per_rpc", "osc.*.max_pages_per_rpc"),
            ("osc max_dirty_mb", "osc.*.max_dirty_mb"),
            ("osc checksums", "osc.*.checksums"),
            ("llite max_read_ahead_mb", "llite.*.max_read_ahead_mb"),
            ("llite max_read_ahead_per_file_mb", "llite.*.max_read_ahead_per_file_mb"),
            ("llite max_read_ahead_whole_mb", "llite.*.max_read_ahead_whole_mb"),
            ("llite statahead_max", "llite.*.statahead_max"))
COLORS = bp.ZONE_COLORS
log, fmt_num, fmt_size = bp.log, bp.fmt_num, bp.fmt_size

EPILOG = """examples:
  lustreperf info                     file system, Lustre client and network details
  lustreperf run-all                  load the dataset if needed, run every test, print the report
  lustreperf start run-all            same, as a background job (survives SSH/run-command disconnects)
  lustreperf status                   progress of the background job
  lustreperf latency --reads 2000     quicker latency run
  lustreperf bandwidth --iterations 1
  lustreperf report                   summary of the latest results (lustre-latest.md/.json/.html)
"""


# --------------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------------

def read_env_file(path: str) -> dict:
    values: dict[str, str] = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip("'\"")
    return values


def parse_bytes(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(k|kb|kib|m|mb|mib|g|gb|gib)?\s*", str(text), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size {text!r} (examples: 65536, 64KiB, 4MiB, 4GiB)")
    unit = (match.group(2) or "").lower()[:1]
    return int(match.group(1)) * {"": 1, "k": KIB, "m": MIB, "g": GIB}[unit]


def fmt_bytes(size: int) -> str:
    return f"{size // GIB} GiB" if size >= GIB and size % GIB == 0 else fmt_size(size)


class LustreConfig:
    """Settings from /etc/blobperf/lustre.env (written by infra/lustre.bicep), overridable by LUSTREPERF_* variables."""

    def __init__(self) -> None:
        self.base = bp.Config()
        self.env_file = os.environ.get("LUSTREPERF_ENV_FILE", ENV_FILE)
        values = read_env_file(self.env_file)
        values.update({k: v for k, v in os.environ.items() if k.startswith("LUSTREPERF_")})
        self.values = values
        self.test_dir = values.get("LUSTREPERF_TEST_DIR") or None
        self.mount_point = values.get("LUSTREPERF_MOUNT_POINT") or "/mnt/amlfs"
        # The file system reports its recommended mount command, e.g.
        # "mount -t lustre -o noatime,user_xattr 10.0.0.4@tcp0:/lustrefs /<local path>".
        command = values.get("LUSTREPERF_MOUNT_COMMAND", "")
        device = re.search(r"(\S+@tcp\d*:/\S+)", command)
        options = re.search(r"\s-o\s+(\S+)", command)
        self.mount_options = values.get("LUSTREPERF_MOUNT_OPTIONS") or (options.group(1) if options else "noatime,user_xattr")
        mgs = values.get("LUSTREPERF_MGS_ADDRESS", "")
        self.device = device.group(1) if device else (f"{mgs}@tcp:/lustrefs" if mgs else "")
        self.root = self.test_dir or os.path.join(self.mount_point, values.get("LUSTREPERF_DIR") or "lustreperf")
        self.filesystem = values.get("LUSTREPERF_FILESYSTEM") or None
        self.sku = values.get("LUSTREPERF_SKU") or None
        capacity = values.get("LUSTREPERF_CAPACITY_TIB", "")
        self.capacity_tib = int(capacity) if capacity.isdigit() else None
        self.zone = values.get("LUSTREPERF_ZONE") or None
        self.mde_exclude = values.get("LUSTREPERF_MDE_EXCLUDE_MOUNT", "true").strip().lower() in ("1", "true", "yes")
        self.workdir = os.path.join(self.base.workdir, "lustre")
        self.results_dir = self.base.results_dir
        self.state_dir = self.base.state_dir
        self.log_dir = self.base.log_dir

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    @property
    def lustre(self) -> bool:
        """False when the tests run on LUSTREPERF_TEST_DIR or the Lustre tools are missing."""
        return not self.test_dir and shutil.which("lctl") is not None

    @property
    def provisioned_mb_s(self) -> int | None:
        """Bandwidth the SKU provisions, e.g. AMLFS-Durable-Premium-500: 500 MB/s per TiB of capacity."""
        match = re.search(r"-(\d+)(?:-|$)", self.sku or "")
        return int(match.group(1)) * self.capacity_tib if match and self.capacity_tib else None


# --------------------------------------------------------------------------------------------
# file system: mount, client details, caches, network
# --------------------------------------------------------------------------------------------

def mount_entry(path: str) -> tuple[str, str, str] | None:
    """(device, file system type, options) of the file system mounted at `path`."""
    found = None
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) >= 4 and fields[1] == path:
                    found = (fields[0], fields[2], fields[3])
    except OSError:
        pass
    return found


def ensure_mounted(lc: LustreConfig) -> None:
    if lc.test_dir:
        os.makedirs(lc.test_dir, exist_ok=True)
        return
    entry = mount_entry(lc.mount_point)
    if entry and entry[1] == "lustre":
        return
    if entry:
        raise SystemExit(f"error: {lc.mount_point} holds a {entry[1]} file system, not Lustre")
    if not lc.device:
        raise SystemExit(f"error: no Lustre file system configured in {lc.env_file}; deploy one first "
                         "(scripts/demo.* lustre-deploy)")
    os.makedirs(lc.mount_point, exist_ok=True)
    log(f"mounting {lc.device} at {lc.mount_point} ({lc.mount_options})")
    result = subprocess.run(["mount", "-t", "lustre", "-o", lc.mount_options, lc.device, lc.mount_point],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
    if result.returncode != 0:
        hint = ""
        if subprocess.run(["modinfo", "lustre"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            hint = (f"; no Lustre client module for the running kernel {platform.release()}: run the setup again "
                    "(scripts/demo.* lustre-deploy), which installs one and reboots if needed")
        raise SystemExit(f"error: mount failed: {result.stdout.strip()}{hint}")


def ensure_mde_exclusions(lc: LustreConfig) -> str:
    """Exclude the RAM-disk work folder and the mount point (random test data only) from Defender for
    Endpoint real-time scanning, so the tests measure the file system rather than antivirus overhead."""
    status = bp.ensure_mde_exclusion(lc.base)
    mdatp = shutil.which("mdatp")
    if not mdatp or not lc.mde_exclude or lc.test_dir:
        return status
    folder = lc.mount_point.rstrip("/") + "/"
    if folder not in bp.run_quiet([mdatp, "exclusion", "list"]):
        bp.run_quiet([mdatp, "exclusion", "folder", "add", "--path", folder])
    excluded = folder in bp.run_quiet([mdatp, "exclusion", "list"])
    return f"{status}; {folder} {'excluded' if excluded else 'could not be excluded'}"


def lctl_values(param: str) -> list[str]:
    output = bp.run_quiet(["lctl", "get_param", "-n", param])
    return [line.strip() for line in output.splitlines() if line.strip() and not line.lstrip().startswith("error")]


def lustre_info(lc: LustreConfig) -> dict:
    """Client and server versions, servers, OST/MDT counts and client tunables of the mounted file system."""
    if not lc.lustre:
        return {"client_version": None}
    version = " ".join(lctl_values("version"))
    client = re.search(r"(\d+\.\d+\.\d+[\w.-]*)", version)
    server = re.search(r"target_version:\s*(\S+)", "\n".join(lctl_values("mdc.*.import")))
    df = bp.run_quiet(["lfs", "df", lc.mount_point])
    entry = mount_entry(lc.mount_point)
    tunables = {}
    for label, param in TUNABLES:
        values = sorted(set(lctl_values(param)), key=lambda v: (len(v), v))
        tunables[label] = ", ".join(values) if values else None
    return {
        "client_version": client.group(1) if client else (version or None),
        "server_version": server.group(1) if server else lc.values.get("LUSTREPERF_SERVER_VERSION") or None,
        "mdts": len(set(re.findall(r"-(MDT[0-9a-f]{4})_UUID", df))),
        "osts": len(set(re.findall(r"-(OST[0-9a-f]{4})_UUID", df))),
        "mds_nids": sorted(set(lctl_values("mdc.*.mds_conn_uuid"))),
        "oss_nids": sorted(set(lctl_values("osc.*.ost_conn_uuid"))),
        "mds_ips": sorted({v.split("@")[0] for v in lctl_values("mdc.*.mds_conn_uuid")}),
        "oss_ips": sorted({v.split("@")[0] for v in lctl_values("osc.*.ost_conn_uuid")}),
        "mount": entry[0] if entry else lc.device,
        "mount_options": entry[2] if entry else lc.mount_options,
        "tunables": tunables,
    }


def filesystem_info(lc: LustreConfig) -> dict:
    info = {"resource": lc.filesystem, "sku": lc.sku, "capacity_tib": lc.capacity_tib,
            "provisioned_mb_s": lc.provisioned_mb_s, "zone": lc.zone, "root": lc.root,
            "test_dir": bool(lc.test_dir)}
    info.update(lustre_info(lc))
    return info


def file_layout(lc: LustreConfig, path: str) -> dict | None:
    """Short summary of a file's Lustre layout: components, patterns (mdt = Data-on-MDT) and stripe counts."""
    if not lc.lustre:
        return None
    text = bp.run_quiet(["lfs", "getstripe", path])
    return {"components": max(1, len(re.findall(r"lcme_id:", text))),
            "patterns": sorted(set(re.findall(r"(?:lmm_)?pattern:\s*(\S+)", text))),
            "stripe_counts": sorted({int(c) for c in re.findall(r"(?:lmm_)?stripe_count:\s*(-?\d+)", text)}),
            "raw": text[:1500]}


def layout_text(layout: dict | None) -> str:
    if not layout:
        return "-"
    patterns = ", ".join(layout.get("patterns") or []) or "?"
    counts = ", ".join(str(c) for c in layout.get("stripe_counts") or []) or "?"
    return f"{layout.get('components')} component(s), pattern {patterns}, stripe count {counts}"


def drop_caches(lc: LustreConfig) -> None:
    """Cold client cache: write back dirty data, drop the page cache and cancel the client's unused DLM locks."""
    os.sync()
    try:
        with open("/proc/sys/vm/drop_caches", "w", encoding="ascii") as handle:
            handle.write("3\n")
    except OSError as exc:
        log(f"WARNING: could not drop the page cache ({exc}); results may include cached reads")
    if lc.lustre:
        bp.run_quiet(["lctl", "set_param", "-n", "ldlm.namespaces.*.lru_size=clear"])


def syncfs(path: str) -> None:
    """Write back the dirty data of the file system that holds `path` and wait for it."""
    fd = os.open(path, os.O_RDONLY)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syncfs(fd) != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"syncfs {path}: {os.strerror(code)}")
    except AttributeError:
        os.sync()
    finally:
        os.close(fd)


WAIT_TIME = re.compile(r"req_waittime\s+(\d+)\s+samples\s+\[usecs?\]\s+\d+\s+\d+\s+(\d+)")


def clear_rpc_stats(lc: LustreConfig) -> None:
    if lc.lustre:
        bp.run_quiet(["lctl", "set_param", "-n", "mdc.*.stats=clear", "osc.*.stats=clear"])


def rpc_stats(lc: LustreConfig, reads: int) -> dict:
    """RPCs since clear_rpc_stats, per server type (mdc = metadata server, osc = storage servers): count,
    RPCs per read and average round trip as the client saw it (network plus server time). Best effort: the
    statistics live in debugfs, which Secure Boot's kernel lockdown hides (all values are then None)."""
    totals = {"mdc": [0, 0], "osc": [0, 0]}
    if lc.lustre:
        current = None
        for line in bp.run_quiet(["lctl", "get_param", "mdc.*.stats", "osc.*.stats"]).splitlines():
            head = re.match(r"(mdc|osc)\.\S*\.stats=", line)
            if head:
                current = head.group(1)
                continue
            match = WAIT_TIME.match(line.strip())
            if match and current:
                totals[current][0] += int(match.group(1))
                totals[current][1] += int(match.group(2))
    return {kind: {"rpcs": count or None, "per_read": round(count / reads, 2) if count and reads else None,
                   "avg_ms": round(total / count / 1000, 3) if count else None}
            for kind, (count, total) in totals.items()}


# --------------------------------------------------------------------------------------------
# workers
# --------------------------------------------------------------------------------------------

def read_whole(path: str, size: int) -> bytes:
    fd = os.open(path, os.O_RDONLY)
    try:
        data = os.read(fd, size)
        while len(data) < size:
            chunk = os.read(fd, size - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) != size:
        raise OSError(errno.EIO, f"short read: {len(data)} of {size} bytes")
    return data


def write_whole(path: str, data: bytes, exclusive: bool) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC), 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def _noop(_: int) -> int:
    return os.getpid()


def _file_task(job: tuple) -> tuple[int, list[str]]:
    """One worker process: `threads` threads share `names`. Returns (files done, first errors).

    create: new random file in target (overwrites, so an interrupted load can resume)
    copy:   source/name -> target/name (the target must not exist yet)
    delete: unlink target/name
    """
    op, source, target, names, size, threads = job
    errors: list[str] = []

    def run(part: list[str]) -> int:
        done = 0
        for name in part:
            try:
                if op == "delete":
                    os.unlink(os.path.join(target, name))
                elif op == "create":
                    write_whole(os.path.join(target, name), os.urandom(size), exclusive=False)
                else:
                    write_whole(os.path.join(target, name), read_whole(os.path.join(source, name), size), exclusive=True)
                done += 1
            except OSError as exc:
                if len(errors) < 10:
                    errors.append(f"{name}: {exc}")
        return done

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        done = sum(pool.map(run, [names[i::threads] for i in range(threads)]))
    return done, errors


def run_files(op: str, source: str | None, target: str, names: list[str], size: int, workers: int) -> dict:
    """Apply `op` to every file with `workers` operations in flight (processes x threads, so the Python
    interpreter lock never limits the rate). The clock starts once every worker process is running."""
    procs = max(1, min(workers, os.cpu_count() or 1, 32))
    threads = max(1, math.ceil(workers / procs))
    jobs = [(op, source, target, names[i::procs], size, threads) for i in range(procs)]
    with multiprocessing.get_context("fork").Pool(processes=procs) as pool:
        pool.map(_noop, range(procs), chunksize=1)
        start = time.perf_counter()
        results = pool.map(_file_task, jobs, chunksize=1)
        seconds = time.perf_counter() - start
    done = sum(r[0] for r in results)
    return {"seconds": round(seconds, 3), "files": done, "errors": len(names) - done,
            "error_samples": [e for r in results for e in r[1]][:10], "workers": procs * threads}


def check_files(result: dict, expected: int, what: str) -> None:
    if result["files"] != expected or result["errors"]:
        raise RuntimeError(f"{what}: {result['files']:,} of {expected:,} files succeeded; "
                           f"{'; '.join(result['error_samples'][:3])}")


def timed_read(path: str, size: int, first_read: int) -> tuple[float, float, float, float, int]:
    """Seconds to open, to the first read (first byte), to the whole file (last byte), and to close it;
    plus the bytes read. With first_read=0 the file is read with one call (first byte = last byte)."""
    start = time.perf_counter()
    fd = os.open(path, os.O_RDONLY)
    opened = time.perf_counter()
    try:
        chunk = os.read(fd, first_read or size)
        first = time.perf_counter()
        got = len(chunk)
        while chunk and got < size:
            chunk = os.read(fd, size - got)
            got += len(chunk)
        last = time.perf_counter()
    finally:
        os.close(fd)
    closed = time.perf_counter()
    return opened - start, first - start, last - start, closed - last, got


def measure_reads(paths: list[str], size: int, first_read: int, threads: int) -> tuple[list, list[str], float]:
    """Timed reads of `paths` by `threads` readers, one file at a time each. Returns (samples, errors, wall seconds)."""
    total = len(paths)
    samples: list = [None] * total
    errors: list[str] = []
    spans: list[tuple[float, float]] = []
    lock = threading.Lock()

    def worker(slot: int) -> None:
        begin = time.perf_counter()
        for k in range(slot, total, threads):
            try:
                samples[k] = timed_read(paths[k], size, first_read)
            except OSError as exc:
                with lock:
                    errors.append(f"{os.path.basename(paths[k])}: {exc}")
        with lock:
            spans.append((begin, time.perf_counter()))

    gc.collect()
    gc.disable()
    try:
        if threads == 1:
            worker(0)
        else:
            pool = [threading.Thread(target=worker, args=(slot,)) for slot in range(threads)]
            for thread in pool:
                thread.start()
            for thread in pool:
                thread.join()
    finally:
        gc.enable()
    wall = max(end for _, end in spans) - min(begin for begin, _ in spans)
    return samples, errors, wall


def run_streams(function, paths: list[str]) -> tuple[float, int]:
    """Run function(index, path) for every path in its own thread, all starting together.
    Returns (wall-clock seconds, sum of the returned byte counts)."""
    barrier = threading.Barrier(len(paths) + 1)
    done = [0] * len(paths)
    failures: list[BaseException] = []

    def wrapper(index: int) -> None:
        try:
            barrier.wait()
            done[index] = function(index, paths[index])
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(len(paths))]
    for thread in threads:
        thread.start()
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        pass
    start = time.perf_counter()
    for thread in threads:
        thread.join()
    seconds = time.perf_counter() - start
    real = [f for f in failures if not isinstance(f, threading.BrokenBarrierError)]
    if failures:
        raise RuntimeError(f"stream failed: {(real or failures)[0]!r}")
    return seconds, sum(done)


def prepare(lc: LustreConfig) -> dict:
    ensure_mounted(lc)
    os.makedirs(lc.root, exist_ok=True)
    os.makedirs(lc.workdir, exist_ok=True)
    os.makedirs(lc.results_dir, exist_ok=True)
    env = bp.environment_info(lc.base)
    env["tool"] = f"lustreperf {VERSION}"
    env["mde_status"] = ensure_mde_exclusions(lc)
    where = lc.test_dir or f"{lc.device} on {lc.mount_point}"
    log(f"{env.get('vm_size')} in {env.get('location')} zone {env.get('zone') or '-'} | {env['cpus']} vCPU | "
        f"kernel {env.get('kernel')} | {where} | {env['mde_status']}")
    return env


def vm_label(env: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", env.get("vm_name") or socket.gethostname())


# --------------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------------

def cmd_info(lc: LustreConfig, args: argparse.Namespace) -> int:
    env = bp.environment_info(lc.base)
    print(f"lustreperf {VERSION} (config {lc.env_file})")
    for key in ("vm_name", "location", "zone", "vm_size", "cpus", "memory_gib", "kernel", "accelerated_networking",
                "nic_drivers", "mde"):
        print(f"  {key:23} {env.get(key)}")
    print(f"  {'file system':23} {lc.filesystem or '-'} | {lc.sku or '-'} | {lc.capacity_tib or '-'} TiB | zone {lc.zone or '-'}"
          f" | provisioned {fmt_num(lc.provisioned_mb_s)} MB/s")
    print(f"  {'device':23} {lc.test_dir or lc.device or '-'} -> {lc.mount_point}")
    try:
        ensure_mounted(lc)
    except (SystemExit, subprocess.SubprocessError, OSError) as exc:
        print(f"  {'mount':23} {exc}")
        return 1
    info = lustre_info(lc)
    if info.get("client_version"):
        print(f"  {'lustre client/server':23} {info['client_version']} / {info.get('server_version')}")
        print(f"  {'servers':23} {info['mdts']} MDT, {info['osts']} OST | MDS {', '.join(info['mds_ips'])} | "
              f"OSS {', '.join(info['oss_ips'])}")
        print(f"  {'mount options':23} {info['mount_options']}")
        print("  tunables                " + " | ".join(f"{k} {v}" for k, v in info["tunables"].items()))
    usage = shutil.disk_usage(lc.root if os.path.isdir(lc.root) else (lc.test_dir or lc.mount_point))
    print(f"  {'capacity':23} {usage.free / KIB ** 4:.2f} TiB free of {usage.total / KIB ** 4:.2f} TiB")
    marker = bp.read_json_file(lc.path(MARKER_DIR, "dataset.json"))
    print(f"  {'dataset':23} " + (f"{marker['objects']:,} x {fmt_bytes(marker['size'])} (loaded {marker.get('loaded_utc')})"
                                   if marker else "not loaded"))
    return 0


def cmd_mount(lc: LustreConfig, args: argparse.Namespace) -> int:
    ensure_mounted(lc)
    log(f"mounted: {lc.test_dir or lc.mount_point}")
    log(f"MDE: {ensure_mde_exclusions(lc)}")
    return 0


def cmd_umount(lc: LustreConfig, args: argparse.Namespace) -> int:
    """Unmount before the file system is deleted, so that no client keeps retrying a server that is gone."""
    if lc.test_dir or not mount_entry(lc.mount_point):
        log(f"{lc.mount_point} is not mounted")
        return 0
    for command in (["umount", lc.mount_point], ["umount", "-f", lc.mount_point], ["umount", "-l", lc.mount_point]):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        if result.returncode == 0:
            log(f"unmounted {lc.mount_point} ({' '.join(command[:-1])})")
            return 0
        log(f"{' '.join(command)}: {result.stdout.strip()}")
    return 1


def cmd_load(lc: LustreConfig, args: argparse.Namespace) -> int:
    """Create the latency dataset: `objects` random files of `size` bytes in one directory, in resumable batches."""
    env = prepare(lc)
    dataset, markers = lc.path(DATASET_DIR), lc.path(MARKER_DIR, "load")
    marker_path = lc.path(MARKER_DIR, "dataset.json")
    expected = {"objects": args.objects, "size": args.size, "naming": bp.NAMING_SCHEME}
    marker = bp.read_json_file(marker_path)
    if marker and all(marker.get(k) == v for k, v in expected.items()) and not args.force:
        log(f"dataset already loaded ({marker['objects']:,} x {fmt_bytes(marker['size'])}), skipping")
        return 0
    os.makedirs(dataset, exist_ok=True)
    os.makedirs(markers, exist_ok=True)
    batches = math.ceil(args.objects / args.batch)
    log(f"loading {args.objects:,} x {fmt_bytes(args.size)} random files into {dataset} "
        f"({batches} batches of up to {args.batch:,}, {args.workers} workers)")
    started = time.perf_counter()
    for batch in range(batches):
        first = batch * args.batch
        count = min(args.batch, args.objects - first)
        batch_marker = os.path.join(markers, f"batch-{batch:05d}.json")
        done = None if args.force else bp.read_json_file(batch_marker)
        if done and (done.get("first"), done.get("count"), done.get("size"), done.get("naming")) == (
                first, count, args.size, bp.NAMING_SCHEME):
            log(f"batch {batch + 1}/{batches}: already loaded")
            continue
        names = [bp.object_name(i) for i in range(first, first + count)]
        result = run_files("create", None, dataset, names, args.size, args.workers)
        check_files(result, count, f"batch {batch + 1}")
        sync_start = time.perf_counter()
        syncfs(dataset)
        seconds = result["seconds"] + time.perf_counter() - sync_start
        bp.write_json_atomic(batch_marker, {"first": first, "count": count, "size": args.size,
                                            "naming": bp.NAMING_SCHEME, "loaded_utc": bp.utc_iso()})
        elapsed = time.perf_counter() - started
        eta = elapsed / (batch + 1) * (batches - batch - 1)
        log(f"batch {batch + 1}/{batches} ({first:,}-{first + count - 1:,}): {seconds:.1f} s "
            f"({count / seconds:,.0f} files/s) | elapsed {bp.fmt_duration(elapsed)}, eta {bp.fmt_duration(eta)}")
    bp.write_json_atomic(marker_path, dict(expected, loaded_utc=bp.utc_iso(), tool_version=VERSION,
                                           vm_name=env.get("vm_name"), layout=file_layout(lc, os.path.join(
                                               dataset, bp.object_name(0)))))
    log(f"dataset loaded in {bp.fmt_duration(time.perf_counter() - started)}")
    return 0


def cmd_latency(lc: LustreConfig, args: argparse.Namespace) -> int:
    """Two passes over different random files of the dataset, one read at a time with a cold cache:
    whole-file reads (open, then one read of the whole file: time to open and to the last byte), then
    first-byte reads (open, then a read of the first `first_read` bytes only: time to the first byte).
    Reading the first bytes separately would add a second data request to the whole-file reads."""
    env = prepare(lc)
    marker = bp.read_json_file(lc.path(MARKER_DIR, "dataset.json"))
    if not marker:
        raise SystemExit("error: no dataset on the file system; run 'lustreperf load' first")
    if marker.get("naming") != bp.NAMING_SCHEME:
        raise SystemExit(f"error: the dataset uses naming {marker.get('naming')!r}; reload with 'lustreperf load --force'")
    population, size = marker["objects"], marker["size"]
    first_read = min(args.first_read, size)
    needed = args.warmup + args.reads * (2 if first_read else 1)
    if needed > population:
        raise SystemExit(f"error: the test reads {needed:,} different files, more than the population ({population:,})")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2 ** 31)
    picks = random.Random(seed).sample(range(population), needed)
    dataset = lc.path(DATASET_DIR)

    def paths_of(indices: list[int]) -> list[str]:
        return [os.path.join(dataset, bp.object_name(i)) for i in indices]

    warmup, measured, first_pass = (picks[:args.warmup], picks[args.warmup:args.warmup + args.reads],
                                    picks[args.warmup + args.reads:])
    paths = paths_of(measured)
    started = bp.utc_iso()
    fs = filesystem_info(lc)
    log(f"{args.reads:,} random whole-file reads of {fmt_bytes(size)} from {population:,} files"
        + (f", then {len(first_pass):,} first-byte reads of {fmt_bytes(first_read)}" if first_read else "")
        + f" ({args.threads} thread{'s' if args.threads > 1 else ''}, seed {seed})")
    drop_caches(lc)
    for path in paths_of(warmup):
        timed_read(path, size, 0)
    clear_rpc_stats(lc)
    samples, errors, wall = measure_reads(paths, size, 0, args.threads)
    rpc = rpc_stats(lc, len(paths))
    first_samples, first_errors = [], []
    if first_read:
        first_samples, first_errors, _ = measure_reads(paths_of(first_pass), first_read, first_read, args.threads)
    ok = [k for k, sample in enumerate(samples) if sample and sample[4] == size]
    first_ok = [k for k, sample in enumerate(first_samples) if sample and sample[4] == first_read]
    errors += [f"{measured[k]}: read {samples[k][4]} bytes" for k, sample in enumerate(samples) if sample and sample[4] != size]
    errors += first_errors
    slowest = sorted(ok, key=lambda k: samples[k][2], reverse=True)[:5]
    stamp = bp.utc_stamp()
    csv_path = os.path.join(lc.results_dir, f"lustre-latency-{stamp}.csv")
    with open(csv_path, "w", encoding="utf-8") as handle:
        handle.write("pass,file,ok,open_ms,first_read_ms,last_byte_ms,close_ms\n")
        for name, indices, rows, expected in (("whole", measured, samples, size), ("first", first_pass, first_samples, first_read)):
            for index, sample in zip(indices, rows):
                if sample:
                    handle.write(f"{name},{index},{int(sample[4] == expected)},{sample[0] * 1000:.3f},{sample[1] * 1000:.3f},"
                                 f"{sample[2] * 1000:.3f},{sample[3] * 1000:.3f}\n")
                else:
                    handle.write(f"{name},{index},0,,,,\n")

    def stats(rows: list, keep: list[int], value) -> dict:
        return bp.summarize([value(rows[k]) * 1000 for k in keep])

    result = {
        "type": "latency", "tool": "lustreperf", "tool_version": VERSION, "started_utc": started, "environment": env,
        "filesystem": fs, "dataset": {"path": dataset, "objects": population, "object_size": size,
                                      "naming": bp.NAMING_SCHEME, "layout": file_layout(lc, paths[0])},
        "reads": args.reads, "first_byte_reads": len(first_pass), "warmup": args.warmup, "threads": args.threads,
        "first_read": first_read, "seed": seed, "rpc": rpc,
        "ok": len(ok), "errors": len(paths) + len(first_pass) - len(ok) - len(first_ok), "error_samples": errors[:10],
        "duration_s": round(wall, 3), "reads_per_s": round(len(ok) / wall, 1) if wall else None,
        "open_ms": stats(samples, ok, lambda s: s[0]), "ttlb_ms": stats(samples, ok, lambda s: s[2]),
        "data_ms": stats(samples, ok, lambda s: s[2] - s[0]), "close_ms": stats(samples, ok, lambda s: s[3]),
        "ttfb_ms": stats(first_samples, first_ok, lambda s: s[1]),
        "slowest": [{"file": measured[k], "ttlb_ms": round(samples[k][2] * 1000, 3)} for k in slowest],
        "samples_csv": os.path.basename(csv_path), "finished_utc": bp.utc_iso(),
    }
    last, first, opened, data = result["ttlb_ms"], result["ttfb_ms"], result["open_ms"], result["data_ms"]
    log(f"last byte avg {fmt_num(last.get('avg'))} ms | p50 {fmt_num(last.get('p50'))} | p99 {fmt_num(last.get('p99'))} | "
        f"p99.9 {fmt_num(last.get('p99_9'))} | max {fmt_num(last.get('max'))} ms | first byte avg {fmt_num(first.get('avg'))} / "
        f"p99 {fmt_num(first.get('p99'))} ms | open avg {fmt_num(opened.get('avg'))} / p99 {fmt_num(opened.get('p99'))} ms | "
        f"read after open avg {fmt_num(data.get('avg'))} ms | errors {result['errors']}")
    if rpc["mdc"]["rpcs"]:
        log(f"RPCs per read: MDS {fmt_num(rpc['mdc']['per_read'])} ({fmt_num(rpc['mdc']['avg_ms'])} ms), "
            f"OSS {fmt_num(rpc['osc']['per_read'])} ({fmt_num(rpc['osc']['avg_ms'])} ms)")
    path = os.path.join(lc.results_dir, f"lustre-latency-{stamp}.json")
    bp.write_json_atomic(path, result)
    log(f"results: {path}")
    bp.publish_results(lc.base, env, [path, csv_path])
    return 0

def time_listing(directory: str, attributes: bool) -> dict:
    """List a directory: names only (readdir), or also each file's attributes (like 'ls -l')."""
    count, total = 0, 0
    start = time.perf_counter()
    with os.scandir(directory) as entries:
        for entry in entries:
            count += 1
            if attributes:
                total += entry.stat(follow_symlinks=False).st_size
    return {"seconds": round(time.perf_counter() - start, 3), "entries": count, "bytes": total if attributes else None}


def cmd_throughput(lc: LustreConfig, args: argparse.Namespace) -> int:
    """Write (copy from the RAM disk + sync), read (copy back, cold cache), list and delete `files` x `size`."""
    env = prepare(lc)
    base = os.path.join(lc.workdir, "throughput")
    source, download = os.path.join(base, "source"), os.path.join(base, "download")
    shutil.rmtree(base, ignore_errors=True)
    log(f"generating {args.files:,} random files of {fmt_bytes(args.size)} in {source}")
    generated = bp.generate_files(source, 0, args.files, args.size)
    names = [bp.object_name(i) for i in range(args.files)]
    stamp = bp.utc_stamp()
    result = {"type": "throughput", "tool": "lustreperf", "tool_version": VERSION, "started_utc": bp.utc_iso(),
              "environment": env, "filesystem": filesystem_info(lc), "files": args.files, "file_size": args.size,
              "iterations": args.iterations, "workers": args.workers, "generate_seconds": round(generated, 3), "runs": []}
    try:
        for iteration in range(1, args.iterations + 1):
            directory = lc.path(f"tp-{vm_label(env)}-{stamp}-{iteration}")
            os.makedirs(directory)
            write = run_files("copy", source, directory, names, args.size, args.workers)
            check_files(write, args.files, "write")
            sync_start = time.perf_counter()
            syncfs(directory)
            write["sync_seconds"] = round(time.perf_counter() - sync_start, 3)
            write["total_seconds"] = round(write["seconds"] + write["sync_seconds"], 3)
            drop_caches(lc)
            shutil.rmtree(download, ignore_errors=True)
            os.makedirs(download)
            read = run_files("copy", directory, download, names, args.size, args.workers)
            check_files(read, args.files, "read")
            drop_caches(lc)
            listing = time_listing(directory, attributes=False)
            drop_caches(lc)
            detailed = time_listing(directory, attributes=True)
            if listing["entries"] != args.files or detailed["bytes"] != args.files * args.size:
                raise RuntimeError(f"list: {listing['entries']:,} entries and {detailed['bytes']:,} bytes, expected "
                                   f"{args.files:,} files of {args.size:,} bytes")
            delete = None
            if not args.keep:
                delete = run_files("delete", None, directory, names, args.size, args.workers)
                check_files(delete, args.files, "delete")
                os.rmdir(directory)
            shutil.rmtree(download, ignore_errors=True)
            result["runs"].append({"iteration": iteration, "directory": directory, "write": write, "read": read,
                                   "list": listing, "list_attributes": detailed, "delete": delete})

            def rate(seconds: float) -> str:
                return f"{seconds:.1f} s ({args.files / seconds:,.0f} files/s)"

            log(f"#{iteration}: write {rate(write['total_seconds'])}, of which sync {write['sync_seconds']:.2f} s | "
                f"read {rate(read['seconds'])} | list {listing['seconds']:.2f} s | list with attributes "
                f"{detailed['seconds']:.2f} s" + (f" | delete {rate(delete['seconds'])}" if delete else ""))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    runs = result["runs"]
    result["summary"] = {"write_s": bp.seconds_stats([r["write"]["total_seconds"] for r in runs]),
                         "read_s": bp.seconds_stats([r["read"]["seconds"] for r in runs]),
                         "list_s": bp.seconds_stats([r["list"]["seconds"] for r in runs]),
                         "list_attributes_s": bp.seconds_stats([r["list_attributes"]["seconds"] for r in runs])}
    if all(r["delete"] for r in runs):
        result["summary"]["delete_s"] = bp.seconds_stats([r["delete"]["seconds"] for r in runs])
    result["finished_utc"] = bp.utc_iso()
    path = os.path.join(lc.results_dir, f"lustre-throughput-{stamp}.json")
    bp.write_json_atomic(path, result)
    log(f"results: {path}")
    bp.publish_results(lc.base, env, [path])
    return 0


def cmd_bandwidth(lc: LustreConfig, args: argparse.Namespace) -> int:
    """Sequential write (with fsync) and cold-cache read of `streams` large files in parallel."""
    if args.file_size % args.block:
        raise SystemExit("error: --file-size must be a multiple of --block")
    env = prepare(lc)
    stamp = bp.utc_stamp()
    directory = lc.path(f"bw-{vm_label(env)}-{stamp}")
    os.makedirs(directory)
    if lc.lustre:
        # Stripe each file over every storage target, the usual layout for large sequential files.
        bp.run_quiet(["lfs", "setstripe", "-c", "-1", directory])
    blocks, total = args.file_size // args.block, args.streams * args.file_size
    tail = memoryview(os.urandom(args.block))[16:]

    def writer(index: int, path: str) -> int:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            for block in range(blocks):
                # A unique 16-byte header per block, so no two blocks are identical.
                if os.writev(fd, [struct.pack("<QQ", index, block), tail]) != args.block:
                    raise OSError(errno.EIO, "short write")
            os.fsync(fd)
        finally:
            os.close(fd)
        return blocks * args.block

    def reader(index: int, path: str) -> int:
        buffer, done = bytearray(args.block), 0
        with open(path, "rb", buffering=0) as handle:
            while True:
                count = handle.readinto(buffer)
                if not count:
                    return done
                done += count

    result = {"type": "bandwidth", "tool": "lustreperf", "tool_version": VERSION, "started_utc": bp.utc_iso(),
              "environment": env, "filesystem": filesystem_info(lc), "streams": args.streams, "file_size": args.file_size,
              "block": args.block, "iterations": args.bw_iterations, "runs": []}
    log(f"{args.streams} streams x {fmt_bytes(args.file_size)} ({total / GIB:.0f} GiB per direction), "
        f"{fmt_bytes(args.block)} I/O, {args.bw_iterations} iteration(s) in {directory}")
    try:
        for iteration in range(1, args.bw_iterations + 1):
            paths = [os.path.join(directory, f"stream-{s:03d}.bin") for s in range(args.streams)]
            write_s, written = run_streams(writer, paths)
            if iteration == 1:
                result["layout"] = file_layout(lc, paths[0])
            drop_caches(lc)
            read_s, read = run_streams(reader, paths)
            if written != total or read != total:
                raise RuntimeError(f"wrote {written:,} and read {read:,} bytes, expected {total:,}")
            for path in paths:
                os.unlink(path)
            run = {"iteration": iteration, "bytes": total, "write_s": round(write_s, 3), "read_s": round(read_s, 3),
                   "write_mb_s": round(total / write_s / 1e6, 1), "read_mb_s": round(total / read_s / 1e6, 1)}
            result["runs"].append(run)
            log(f"#{iteration}: write {run['write_mb_s']:,.0f} MB/s ({write_s:.1f} s, including fsync) | "
                f"read {run['read_mb_s']:,.0f} MB/s ({read_s:.1f} s)")
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    for key in ("write_mb_s", "read_mb_s"):
        values = [r[key] for r in result["runs"]]
        result[key] = {"best": max(values), "median": round(bp.percentile(sorted(values), 50), 1), "runs": values}
    result["provisioned_mb_s"] = lc.provisioned_mb_s
    result["finished_utc"] = bp.utc_iso()
    path = os.path.join(lc.results_dir, f"lustre-bandwidth-{stamp}.json")
    bp.write_json_atomic(path, result)
    log(f"results: {path}")
    bp.publish_results(lc.base, env, [path])
    return 0


def cmd_run_all(lc: LustreConfig, args: argparse.Namespace) -> int:
    started = time.perf_counter()
    steps = (("latency dataset", cmd_load), ("latency test", cmd_latency), ("small-file throughput test", cmd_throughput),
             ("large-file bandwidth test", cmd_bandwidth), ("report", cmd_report))
    for number, (title, command) in enumerate(steps, 1):
        log(f"=== step {number}/{len(steps)}: {title} ===")
        command(lc, args)
    log(f"=== all done in {bp.fmt_duration(time.perf_counter() - started)} ===")
    return 0


def remove_tree(path: str, workers: int) -> None:
    """Delete a directory; the files of a large flat directory in parallel."""
    files = [entry.name for entry in os.scandir(path) if entry.is_file(follow_symlinks=False)]
    if len(files) > 1000:
        run_files("delete", None, path, files, 0, workers)
    shutil.rmtree(path, ignore_errors=True)


def cmd_cleanup(lc: LustreConfig, args: argparse.Namespace) -> int:
    ensure_mounted(lc)
    targets = sorted(p for p in glob.glob(lc.path("tp-*")) + glob.glob(lc.path("bw-*")) if os.path.isdir(p))
    if args.dataset:
        targets += [p for p in (lc.path(DATASET_DIR), lc.path(MARKER_DIR)) if os.path.isdir(p)]
    for path in targets:
        remove_tree(path, args.workers)
    log(f"deleted {len(targets)} folder(s){': ' + ', '.join(os.path.basename(p) for p in targets) if targets else ''}")
    return 0


# --------------------------------------------------------------------------------------------
# reporting (separate lustre-* report files; never mixed into the blob report)
# --------------------------------------------------------------------------------------------

def latest_local(lc: LustreConfig, kind: str) -> str | None:
    files = sorted(glob.glob(os.path.join(lc.results_dir, f"lustre-{kind}-*[0-9]z.json")))
    return files[-1] if files else None


def collect_published(lc: LustreConfig, since: str | None) -> dict[str, list[dict]]:
    """Results that the zonal VMs published to the shared results container, optionally from `since` on."""
    target = bp.results_target(lc.base)
    if target is None:
        raise SystemExit("error: no account configured for the results container (BLOBPERF_RESULTS_TARGET)")
    runs: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    client = lc.base.client(target)
    try:
        for name in bp.list_blob_names(client, bp.RESULTS_CONTAINER):
            match = RESULT_NAME.fullmatch(name.rpartition("/")[2])
            if not match:
                continue
            run = bp.get_json_blob(client, bp.RESULTS_CONTAINER, name)
            if not run or not (run.get("environment") or {}).get("zone"):
                continue
            if since and run.get("started_utc", "") < since:
                continue
            runs[match.group(1)].append(run)
    finally:
        client.close()
    return runs


def zone_of(run: dict) -> str:
    return str((run.get("environment") or {}).get("zone") or "-")


def _min(values) -> float | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def build_summary(runs: dict[str, list[dict]], zone_map: dict, since: str | None, mode: str) -> dict:
    """Best run per zone (lowest time, highest bandwidth); every run stays in per_run / runs for the spread."""
    everything = sorted((r for kind in KINDS for r in runs[kind]), key=lambda r: r["started_utc"])
    if not everything:
        raise SystemExit("error: no Lustre results to report" + (f" since {since}" if since else ""))
    env = everything[-1].get("environment") or {}
    summary = {
        "type": "lustre", "mode": mode, "generated_utc": bp.utc_iso(), "tool_version": VERSION, "since": since,
        "zones": sorted({zone_of(r) for r in everything}), "zone_map": zone_map,
        "filesystem": dict(everything[-1].get("filesystem") or {}),
        "vms": {zone_of(r): (r.get("environment") or {}).get("vm_name") for r in everything},
        "environment": {key: env.get(key) for key in ("location", "vm_size", "cpus", "memory_gib", "image", "kernel",
                                                       "accelerated_networking", "nic_drivers")},
        "mde_status": sorted({(r.get("environment") or {}).get("mde_status") or "unknown" for r in everything}),
        "latency": {}, "throughput": {}, "bandwidth": {}, "sources": [],
    }
    for run in sorted(runs["latency"], key=lambda r: r["started_utc"]):
        rpc = run.get("rpc") or {}
        entry = summary["latency"].setdefault(zone_of(run), {"per_run": []})
        entry.update(objects=run["dataset"]["objects"], object_size=run["dataset"]["object_size"],
                     layout=run["dataset"].get("layout"), first_read=run.get("first_read"), threads=run.get("threads"))
        entry["per_run"].append({
            "started_utc": run["started_utc"], "reads": run["reads"], "errors": run["errors"],
            "ttlb_ms": {k: run["ttlb_ms"].get(k) for k in LATENCY_KEYS},
            "ttfb_ms": {k: run["ttfb_ms"].get(k) for k in LATENCY_KEYS},
            "open_ms": {k: run["open_ms"].get(k) for k in LATENCY_KEYS},
            "data_ms": {k: (run.get("data_ms") or {}).get(k) for k in LATENCY_KEYS},

            "rpc_mds_ms": (rpc.get("mdc") or {}).get("avg_ms"), "rpc_oss_ms": (rpc.get("osc") or {}).get("avg_ms"),
            "rpcs_per_read_mds": (rpc.get("mdc") or {}).get("per_read"),
            "rpcs_per_read_oss": (rpc.get("osc") or {}).get("per_read")})
    for entry in summary["latency"].values():
        per_run = entry["per_run"]
        entry.update(runs=len(per_run), reads_per_run=per_run[0]["reads"], errors=sum(r["errors"] for r in per_run))
        for metric in ("ttlb_ms", "ttfb_ms", "open_ms", "data_ms"):
            entry[metric] = {k: _min(r[metric][k] for r in per_run) for k in LATENCY_KEYS}
        for key in ("rpc_mds_ms", "rpc_oss_ms", "rpcs_per_read_mds", "rpcs_per_read_oss"):
            entry[key] = _min(r[key] for r in per_run)
    for run in sorted(runs["throughput"], key=lambda r: r["started_utc"]):
        entry = summary["throughput"].setdefault(zone_of(run), {"files": run["files"], "file_size": run["file_size"],
                                                                "workers": run.get("workers"), "values": {}})
        for item in run["runs"]:
            values = {"write": item["write"]["total_seconds"], "read": item["read"]["seconds"],
                      "list": item["list"]["seconds"], "list_attributes": item["list_attributes"]["seconds"],
                      "delete": (item.get("delete") or {}).get("seconds")}
            for op, value in values.items():
                if value is not None:
                    entry["values"].setdefault(op, []).append(value)
    for entry in summary["throughput"].values():
        for op, values in entry.pop("values").items():
            entry[f"{op}_s"] = bp.seconds_stats(values)
    for run in sorted(runs["bandwidth"], key=lambda r: r["started_utc"]):
        entry = summary["bandwidth"].setdefault(zone_of(run), {"streams": run["streams"], "file_size": run["file_size"],
                                                               "block": run["block"], "layout": run.get("layout"),
                                                               "write": [], "read": []})
        entry["write"] += [r["write_mb_s"] for r in run["runs"]]
        entry["read"] += [r["read_mb_s"] for r in run["runs"]]
    for entry in summary["bandwidth"].values():
        for op in ("write", "read"):
            values = entry.pop(op)
            entry[f"{op}_mb_s"] = {"best": max(values), "median": round(bp.percentile(sorted(values), 50), 1), "runs": values}
    return summary


def zone_name(summary: dict, zone: str) -> str:
    """'Zone 1 (az2)', or 'Zone 1 (az2, AMLFS)' for the file system's zone; the VM name for a VM without a zone."""
    if zone == "-":
        return summary["vms"].get(zone) or "VM"
    label = bp.zone_label(summary, zone)
    if str(summary["filesystem"].get("zone") or "") != zone:
        return label
    return label[:-1] + ", AMLFS)" if label.endswith(")") else f"{label} (AMLFS)"


def _first(summary: dict, section: str, key: str):
    return next((entry.get(key) for entry in summary[section].values() if entry.get(key) is not None), None)


def zone_effects(summary: dict) -> list[dict]:
    """Best and worst zone for the headline metrics, plus which zone won each latency round."""
    zones, latency, throughput, bandwidth = summary["zones"], summary["latency"], summary["throughput"], summary["bandwidth"]
    files = _first(summary, "throughput", "files") or 0

    def lat(metric: str, key: str):
        return (lambda z: latency.get(z, {}).get(metric, {}).get(key)), (lambda r: r[metric][key])

    def lat_value(key: str):
        return (lambda z: latency.get(z, {}).get(key)), (lambda r: r[key])

    metrics = [("Average latency, last byte (ms)", *lat("ttlb_ms", "avg"), False),
               ("P99 latency, last byte (ms)", *lat("ttlb_ms", "p99"), False),
               ("Average latency, first byte (ms)", *lat("ttfb_ms", "avg"), False),
               ("Average open, metadata server (ms)", *lat("open_ms", "avg"), False),
               ("Average read after open, storage server (ms)", *lat("data_ms", "avg"), False),
               ("Metadata server request round trip (ms)", *lat_value("rpc_mds_ms"), False),
               ("Storage server request round trip (ms)", *lat_value("rpc_oss_ms"), False)]
    for op, label in FILE_OPS:
        metrics.append((f"{label}, {files:,} files (s)",
                        lambda z, o=op: throughput.get(z, {}).get(f"{o}_s", {}).get("min"), None, False))
    for op in ("write", "read"):
        metrics.append((f"Large files, {op} (MB/s)",
                        lambda z, o=op: bandwidth.get(z, {}).get(f"{o}_mb_s", {}).get("best"), None, True))
    effects = []
    for label, get, per_run_value, higher in metrics:
        values = {z: get(z) for z in zones if get(z) is not None}
        if len(values) < 2:
            continue
        best = (max if higher else min)(values, key=values.get)
        worst = (min if higher else max)(values, key=values.get)
        ratio = values[best] / values[worst] if higher else values[worst] / values[best]
        effect = {"metric": label, "higher_is_better": higher, "best": best, "best_value": values[best],
                  "worst": worst, "worst_value": values[worst], "difference_pct": round((ratio - 1) * 100, 1)}
        if per_run_value:
            per_zone = {z: latency[z]["per_run"] for z in values}
            rounds = min(len(r) for r in per_zone.values())
            effect["round_winners"] = [min(per_zone, key=lambda z, i=i: per_run_value(per_zone[z][i])
                                           if per_run_value(per_zone[z][i]) is not None else math.inf)
                                       for i in range(rounds)]
        effects.append(effect)
    return effects


def _plural(runs: str) -> str:
    return "run" if runs == "1" else "runs"


def _runs_text(counts: set) -> str:
    return "/".join(f"{c:,}" for c in sorted(counts)) if counts else "0"


def zone_tables(summary: dict) -> list[tuple[str, list[str], list[list], list[bool | None], str]]:
    """[(title, columns, one row per zone, per column: True/False = higher/lower is better, None = no ranking, note)]
    with each zone's best run."""
    zones, tables = summary["zones"], []
    latency, throughput, bandwidth = summary["latency"], summary["throughput"], summary["bandwidth"]
    if latency:
        entries = list(latency.values())
        runs, reads = _runs_text({e["runs"] for e in entries}), _runs_text({e["reads_per_run"] for e in entries})
        size = fmt_bytes(entries[0]["object_size"])
        population = f"{entries[0]['objects']:,}"
        columns = ["avg", "P50", "P90", "P99", "P99.9"]
        keys = ("avg", "p50", "p90", "p99", "p99_9")
        tables.append((f"Latency to last byte (ms): best of {runs} {_plural(runs)} per zone (each run: {reads} random {size} files "
                       f"of {population}, one at a time, cold client cache)", columns,
                       [[latency.get(z, {}).get("ttlb_ms", {}).get(k) for k in keys] for z in zones], [False] * 5,
                       "Each value is the lowest that metric reached in any run of the zone. A read is timed from "
                       "open() until the whole file has arrived."))
        first_read = entries[0].get("first_read")
        tables.append(("First byte, open and read after open (ms): best of the same runs",
                       ["First byte avg", "First byte P99", "Open avg", "Open P99", "Read after open avg",
                        "Read after open P99"],
                       [[latency.get(z, {}).get(m, {}).get(k) for m in ("ttfb_ms", "open_ms", "data_ms") for k in ("avg", "p99")]
                        for z in zones], [False] * 6,
                       f"First byte: open() plus a read of the first {fmt_bytes(first_read) if first_read else 'bytes'}, "
                       "measured on as many other random files in a second pass. Open: open() alone, a round trip to the "
                       "metadata server. Read after open: last byte minus open, the data request to the storage server."))
        keys = ("rpc_mds_ms", "rpc_oss_ms", "rpcs_per_read_mds", "rpcs_per_read_oss")
        tables.append(("Request round trips (ms): best of the same runs",
                       ["MDS RPC", "OSS RPC", "MDS RPCs/read", "OSS RPCs/read"],
                       [[latency.get(z, {}).get(k) for k in keys] for z in zones], [False] * 2 + [None] * 2,
                       "The client's average request round trip to the metadata server (MDS) and the storage servers "
                       "(OSS) during the run, network plus server time, and requests per file read. Only where the kernel "
                       "allows reading Lustre's debug statistics: Secure Boot's lockdown does not."))
    if throughput:
        entries = list(throughput.values())
        runs = _runs_text({len(e["write_s"]["runs"]) for e in entries})
        files, size = entries[0]["files"], fmt_bytes(entries[0]["file_size"])
        tables.append((f"Small files (seconds): best of {runs} {_plural(runs)} per zone, {files:,} x {size} files, "
                       f"{entries[0].get('workers')} in flight", [label for _, label in FILE_OPS],
                       [[throughput.get(z, {}).get(f"{op}_s", {}).get("min") for op, _ in FILE_OPS] for z in zones],
                       [False] * len(FILE_OPS),
                       "Write: copy from a RAM disk, then sync until the servers have the data. Read: copy back to the "
                       "RAM disk with a cold cache. List names: readdir only. List with attributes: readdir and stat "
                       "of every file, like 'ls -l' (the closest match to the blob demo's List Blobs, which returns "
                       "properties too)."))
    if bandwidth:
        entries = list(bandwidth.values())
        runs = _runs_text({len(e["write_mb_s"]["runs"]) for e in entries})
        provisioned = summary["filesystem"].get("provisioned_mb_s")
        tables.append((f"Large files (MB/s): best of {runs} {_plural(runs)} per zone, {entries[0]['streams']} streams x "
                       f"{fmt_bytes(entries[0]['file_size'])}, {fmt_bytes(entries[0]['block'])} I/O", ["Write", "Read"],
                       [[bandwidth.get(z, {}).get(f"{op}_mb_s", {}).get("best") for op in ("write", "read")] for z in zones],
                       [True, True],
                       "Higher is better. Write includes fsync; read starts with a cold cache. 1 MB = 1,000,000 bytes."
                       + (f" Provisioned file system bandwidth: {provisioned:,} MB/s." if provisioned else "")))
    # Columns without any value (e.g. RPC statistics that kernel lockdown hides) are left out.
    result = []
    for title, columns, rows, higher, note in tables:
        keep = [c for c in range(len(columns)) if any(row[c] is not None for row in rows)]
        if keep:
            result.append((title, [columns[c] for c in keep], [[row[c] for c in keep] for row in rows],
                           [higher[c] for c in keep], note))
    return result


def run_tables(summary: dict) -> list[tuple[str, list[str], list[list[str]]]]:
    """Every individual run, so the spread behind the best values stays visible."""
    zones, tables = summary["zones"], []
    if summary["latency"]:
        rows = []
        for zone in zones:
            for run in summary["latency"].get(zone, {}).get("per_run", []):
                rows.append([run["started_utc"], zone_name(summary, zone),
                             f"{fmt_num(run['ttlb_ms']['avg'])} / {fmt_num(run['ttlb_ms']['p99'])}",
                             f"{fmt_num(run['ttfb_ms']['avg'])} / {fmt_num(run['ttfb_ms']['p99'])}",
                             f"{fmt_num(run['open_ms']['avg'])} / {fmt_num(run['open_ms']['p99'])}",
                             f"{fmt_num(run['data_ms']['avg'])} / {fmt_num(run['data_ms']['p99'])}"])
        tables.append(("Latency per run (avg / P99 ms, in time order)",
                       ["Started (UTC)", "Zone", "Last byte", "First byte", "Open", "Read after open"], sorted(rows)))
    if summary["throughput"]:
        rows = []
        for zone in zones:
            entry = summary["throughput"].get(zone)
            if not entry:
                continue
            for i in range(len(entry["write_s"]["runs"])):
                rows.append([zone_name(summary, zone), str(i + 1)] +
                            [fmt_num(entry[f"{op}_s"]["runs"][i]) if f"{op}_s" in entry and i < len(entry[f"{op}_s"]["runs"])
                             else "-" for op, _ in FILE_OPS])
        tables.append(("Small files per run (seconds)", ["Zone", "Run", *[label for _, label in FILE_OPS]], rows))
    if summary["bandwidth"]:
        rows = []
        for zone in zones:
            entry = summary["bandwidth"].get(zone)
            if entry:
                rows += [[zone_name(summary, zone), str(i + 1), fmt_num(w), fmt_num(r)]
                         for i, (w, r) in enumerate(zip(entry["write_mb_s"]["runs"], entry["read_mb_s"]["runs"]))]
        tables.append(("Large files per run (MB/s)", ["Zone", "Run", "Write", "Read"], rows))
    return tables


def _title(summary: dict) -> str:
    return "Azure Managed Lustre performance" + (" by availability zone" if summary["mode"] == "zones" else "")


def _header(summary: dict) -> str:
    fs, env = summary["filesystem"], summary["environment"]
    where = f"zone {fs['zone']}" if fs.get("zone") else "no zone"
    physical = summary.get("zone_map", {}).get(str(fs.get("zone") or ""))
    if physical:
        where += f" ({physical.rsplit('-', 1)[-1]})"
    return (f"{summary['generated_utc']} | {env.get('location')} | {fs.get('sku') or 'test directory'} "
            f"{fs.get('capacity_tib') or '-'} TiB in {where}, {fmt_num(fs.get('provisioned_mb_s'))} MB/s provisioned | "
            f"Lustre client {fs.get('client_version') or '-'}, server {fs.get('server_version') or '-'} | "
            f"{env.get('vm_size')} ({env.get('cpus')} vCPU) | lustreperf {summary['tool_version']}")


def _intro(summary: dict) -> str:
    text = ("Tests of the Premium vs Standard block blob demo, run against Azure Managed Lustre, plus a large-file "
            "bandwidth test. Lower is better except for bandwidth.")
    if summary["mode"] == "zones":
        text += (" One identical VM per availability zone, all using the same file system (AMLFS marks its zone). Every "
                 "test ran several times per zone, one zone at a time, with latency rounds rotating the zone order; the "
                 "tables show each zone's best run.")
    return text


def _best_cells(rows: list[list], higher: list[bool | None]) -> list:
    best = []
    for column, high in enumerate(higher):
        values = [row[column] for row in rows if row[column] is not None]
        best.append((max(values) if high else min(values)) if values and high is not None else None)
    return best


def render_markdown(summary: dict) -> str:
    zones, multi = summary["zones"], len(summary["zones"]) > 1
    lines = [f"# {_title(summary)}", "", _header(summary), "", _intro(summary)]
    effects = zone_effects(summary)
    if effects:
        lines += ["", "## Zone effect", "", "| Metric | Best zone | Worst zone | Difference | Best zone per round |",
                  "|---|---|---|---:|---|"]
        for x in effects:
            lines.append(f"| {x['metric']} | {zone_name(summary, x['best'])}: {fmt_num(x['best_value'])} | "
                         f"{zone_name(summary, x['worst'])}: {fmt_num(x['worst_value'])} | {x['difference_pct']:.0f}% | "
                         f"{', '.join(x.get('round_winners', [])) or '-'} |")
    for title, columns, rows, higher, note in zone_tables(summary):
        best = _best_cells(rows, higher)
        lines += ["", f"## {title}", "", "| Zone | " + " | ".join(columns) + " |", "|---|" + "---:|" * len(columns)]
        for zone, row in zip(zones, rows):
            cells = [f"**{fmt_num(v)}**" if multi and v is not None and v == best[c] else fmt_num(v) for c, v in enumerate(row)]
            lines.append(f"| {zone_name(summary, zone)} | " + " | ".join(cells) + " |")
        lines += ["", note]
    for title, columns, rows in run_tables(summary):
        lines += ["", f"## {title}", "", "| " + " | ".join(columns) + " |", "|---|---|" + "---:|" * (len(columns) - 2)]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def _chart_rows(summary: dict) -> list[list[str]]:
    zones, latency, throughput, bandwidth = summary["zones"], summary["latency"], summary["throughput"], summary["bandwidth"]
    colour = {z: COLORS[i % len(COLORS)] for i, z in enumerate(zones)}
    rows: list[list[str]] = []
    if latency:
        series = [(zone_name(summary, z), colour[z], [latency.get(z, {}).get("ttlb_ms", {}).get(k)
                                                      for k in ("avg", "p50", "p90", "p99")]) for z in zones]
        chart = [bp.svg_bar_chart("Latency to last byte by zone (best run)", "milliseconds", ["Average", "P50", "P90", "P99"], series)]
        series = [(zone_name(summary, z), colour[z], [latency.get(z, {}).get(m, {}).get("avg")
                                                      for m in ("open_ms", "ttfb_ms", "ttlb_ms")]) for z in zones]
        chart.append(bp.svg_bar_chart("Average latency by zone (best run)", "milliseconds", ["Open", "First byte", "Last byte"], series))
        rows.append(chart)
        trips = [(label, key) for label, key in (("MDS request", "rpc_mds_ms"), ("OSS request", "rpc_oss_ms"))
                 if any(latency.get(z, {}).get(key) is not None for z in zones)]
        if trips:
            series = [(zone_name(summary, z), colour[z], [latency.get(z, {}).get(key) for _, key in trips]) for z in zones]
            rows.append([bp.svg_bar_chart("Round trips by zone (best run)", "milliseconds", [label for label, _ in trips],
                                          series)])
    chart = []
    if throughput:
        files = _first(summary, "throughput", "files") or 0
        # Listing names takes a fraction of a second: too short to read next to the others (it is in the table).
        ops = [(op, label) for op, label in FILE_OPS if op != "list"]
        series = [(zone_name(summary, z), colour[z], [throughput.get(z, {}).get(f"{op}_s", {}).get("min") for op, _ in ops])
                  for z in zones]
        chart.append(bp.svg_bar_chart(f"{files:,} small files by zone (best run)", "seconds",
                                      ["Write", "Read", "List + attributes", "Delete"], series))
    if bandwidth:
        series = [(zone_name(summary, z), colour[z], [bandwidth.get(z, {}).get(f"{op}_mb_s", {}).get("best")
                                                      for op in ("write", "read")]) for z in zones]
        provisioned = summary["filesystem"].get("provisioned_mb_s")
        title = "Large files by zone, best run" + (f" (provisioned {provisioned:,})" if provisioned else "")
        chart.append(bp.svg_bar_chart(title, "MB/s, higher is better", ["Write", "Read"], series,
                                      ymax=bp.nice_ceiling(max([provisioned or 0] + [v for _, _, vals in series
                                                                                      for v in vals if v]))))
    rows.append(chart)
    return rows


def render_html(summary: dict) -> str:
    e, zones, multi = html.escape, summary["zones"], len(summary["zones"]) > 1
    tables = []
    effects = zone_effects(summary)
    if effects:
        body = "".join(
            f"<tr><th>{e(x['metric'])}</th><td>{e(zone_name(summary, x['best']))}: {fmt_num(x['best_value'])}</td>"
            f"<td>{e(zone_name(summary, x['worst']))}: {fmt_num(x['worst_value'])}</td><td>{x['difference_pct']:.0f}%</td>"
            f"<td>{e(', '.join(x.get('round_winners', [])) or '-')}</td></tr>" for x in effects)
        tables.append("<h2>Zone effect</h2><table class='left'><tr><th>Metric</th><th>Best zone</th><th>Worst zone</th>"
                      f"<th>Difference</th><th>Best zone per round</th></tr>{body}</table>")
    for title, columns, rows, higher, note in zone_tables(summary):
        best = _best_cells(rows, higher)
        body = "".join(
            f"<tr><th>{e(zone_name(summary, z))}</th>" + "".join(
                f"<td class='best'>{fmt_num(v)}</td>" if multi and v is not None and v == best[c] else f"<td>{fmt_num(v)}</td>"
                for c, v in enumerate(row)) + "</tr>" for z, row in zip(zones, rows))
        tables.append(f"<h2>{e(title)}</h2><table><tr><th>Zone</th>{''.join(f'<th>{e(c)}</th>' for c in columns)}</tr>"
                      f"{body}</table><p class='muted'>{e(note)}</p>")
    for title, columns, rows in run_tables(summary):
        body = "".join("<tr>" + "".join(f"<td>{e(cell)}</td>" for cell in row) + "</tr>" for row in rows)
        tables.append(f"<h2>{e(title)}</h2><table><tr>{''.join(f'<th>{e(c)}</th>' for c in columns)}</tr>{body}</table>")
    fs = summary["filesystem"]
    layouts = {entry.get("layout") and layout_text(entry["layout"]) for entry in summary["latency"].values()}
    big = {entry.get("layout") and layout_text(entry["layout"]) for entry in summary["bandwidth"].values()}
    details = {
        "file system": f"{fs.get('resource') or '-'} ({fs.get('sku') or '-'}, {fs.get('capacity_tib') or '-'} TiB, zone "
                       f"{fs.get('zone') or '-'})",
        "provisioned bandwidth": f"{fmt_num(fs.get('provisioned_mb_s'))} MB/s",
        "servers": f"{fs.get('mdts', '-')} MDT, {fs.get('osts', '-')} OST; MDS {', '.join(fs.get('mds_ips') or [])}; "
                   f"OSS {', '.join(fs.get('oss_ips') or [])}",
        "lustre client / server": f"{fs.get('client_version') or '-'} / {fs.get('server_version') or '-'}",
        "mount": f"{fs.get('mount') or '-'} ({fs.get('mount_options') or '-'})",
        "small-file layout": ", ".join(sorted(x for x in layouts if x)) or "-",
        "large-file layout": ", ".join(sorted(x for x in big if x)) or "-",
        "client tunables": "; ".join(f"{k} {v}" for k, v in (fs.get("tunables") or {}).items()),
    }
    env = dict(summary["environment"], mde=", ".join(summary["mde_status"]),
               vms=", ".join(f"{zone_name(summary, z)}: {vm}" for z, vm in sorted(summary["vms"].items())))
    rows = "".join(f"<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>" for k, v in details.items())
    rows += "".join(f"<tr><th>{e(k.replace('_', ' '))}</th><td>{e(str(v))}</td></tr>" for k, v in env.items())
    intro = f"{e(_header(summary))}<br>{e(_intro(summary))}"
    if multi:
        intro += " Highlighted = best zone."
    content = f"{bp.chart_html(_chart_rows(summary))}{''.join(tables)}<h2>Environment</h2><table class='env'>{rows}</table>"
    return bp.html_page(_title(summary), intro, content)


def write_report(lc: LustreConfig, summary: dict, printed: str) -> None:
    """Save lustre-report-<stamp>.{json,md,html} plus lustre-latest.* copies (picked up by `export`)."""
    stamp, markdown = bp.utc_stamp(), render_markdown(summary)
    bp.write_json_atomic(os.path.join(lc.results_dir, f"lustre-report-{stamp}.json"), summary)
    for suffix, content in (("md", markdown), ("html", render_html(summary))):
        with open(os.path.join(lc.results_dir, f"lustre-report-{stamp}.{suffix}"), "w", encoding="utf-8") as handle:
            handle.write(content)
    for suffix in ("json", "md", "html"):
        shutil.copyfile(os.path.join(lc.results_dir, f"lustre-report-{stamp}.{suffix}"),
                        os.path.join(lc.results_dir, f"lustre-latest.{suffix}"))
    if printed == "md":
        print(markdown, flush=True)
    elif printed == "json":
        print(json.dumps(summary, indent=2), flush=True)


def cmd_report(lc: LustreConfig, args: argparse.Namespace) -> int:
    printed = getattr(args, "print", "md")
    if getattr(args, "zones", False):
        summary = build_summary(collect_published(lc, args.since), bp.parse_zone_map(args.zone_map), args.since, "zones")
    else:
        paths = {kind: latest_local(lc, kind) for kind in KINDS}
        runs = {kind: [run] if (run := bp.read_json_file(path)) else [] for kind, path in paths.items()}
        summary = build_summary(runs, bp.parse_zone_map(getattr(args, "zone_map", None)), None, "single")
        summary["sources"] = [os.path.basename(p) for kind, p in paths.items() if p and runs[kind]]
        summary["sources"] += [r["samples_csv"] for r in runs["latency"] if r.get("samples_csv")]
    write_report(lc, summary, printed)
    return 0


# --------------------------------------------------------------------------------------------
# background jobs (used by scripts/demo.* through `az vm run-command`, or over SSH)
# --------------------------------------------------------------------------------------------

def job_path(lc: LustreConfig) -> str:
    return os.path.join(lc.state_dir, "lustre-job.json")


def finish_job(lc: LustreConfig, job_id: str, exit_code: int) -> None:
    job = bp.read_json_file(job_path(lc))
    if job and job.get("id") == job_id:
        job.update(state="succeeded" if exit_code == 0 else "failed", exit_code=exit_code, finished_utc=bp.utc_iso())
        bp.write_json_atomic(job_path(lc), job)


def cmd_start(lc: LustreConfig, args: argparse.Namespace) -> int:
    command = list(args.job_command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or command[0] in ("start", "status", "export") or command[0].startswith("-"):
        raise SystemExit("error: usage: lustreperf start <command> [options], e.g. lustreperf start run-all")
    if os.geteuid() != 0:
        raise SystemExit("error: 'lustreperf start' needs root (sudo)")
    # One job per VM at a time, blob or Lustre, so the two never compete for the network.
    for path, tool in ((job_path(lc), "lustreperf"), (bp.job_path(lc.base), "blobperf")):
        job = bp.read_json_file(path)
        if job and job.get("state") == "running" and bp.unit_active(job.get("unit", "")):
            raise SystemExit(f"error: {tool} job {job['id']} is still running (see '{tool} status')")
    os.makedirs(lc.log_dir, exist_ok=True)
    job_id = bp.utc_stamp()
    unit = f"lustreperf-{job_id}"
    log_file = os.path.join(lc.log_dir, f"lustre-job-{job_id}.log")
    bp.write_json_atomic(job_path(lc), {"id": job_id, "unit": unit, "command": command, "log": log_file,
                                        "state": "running", "started_utc": bp.utc_iso()})
    passthrough = [f"--setenv={k}={v}" for k, v in os.environ.items() if k.startswith(("BLOBPERF_", "LUSTREPERF_"))]
    subprocess.run(["systemd-run", f"--unit={unit}", "--collect", "--quiet",
                    f"--property=StandardOutput=append:{log_file}", f"--property=StandardError=append:{log_file}",
                    "--setenv=PYTHONUNBUFFERED=1", *passthrough,
                    sys.executable, os.path.abspath(__file__), "--job-id", job_id, *command], check=True)
    print(f"started job {job_id}: lustreperf {' '.join(command)}\nlog: {log_file}")
    return 0


def cmd_status(lc: LustreConfig, args: argparse.Namespace) -> int:
    job = bp.read_json_file(job_path(lc))
    if not job:
        print("JOB - STATE: none")
        return 0
    state, note = job.get("state", "unknown"), ""
    if state == "running" and not bp.unit_active(job.get("unit", "")):
        state, note = "failed", " (process ended unexpectedly)"
    started = dt.datetime.strptime(job["started_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    finished = job.get("finished_utc")
    end = (dt.datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
           if finished else dt.datetime.now(dt.timezone.utc))
    print(f"JOB {job['id']} STATE: {state}{note} ELAPSED: {bp.fmt_duration((end - started).total_seconds())} "
          f"CMD: lustreperf {' '.join(job.get('command', []))}")
    for line in bp.tail(job.get("log", ""), args.lines):
        print(line[:args.width])
    return 0


def cmd_export(lc: LustreConfig, args: argparse.Namespace) -> int:
    """Print the latest Lustre report bundle (tar.gz, base64) in chunks small enough for run-command output."""
    bundle = os.path.join(lc.state_dir, "export", "lustre-bundle.tgz")
    if args.chunk == 0 or not os.path.exists(bundle):
        summary = bp.read_json_file(os.path.join(lc.results_dir, "lustre-latest.json"))
        if not summary:
            raise SystemExit("error: no Lustre report yet; run 'lustreperf report' first")
        names = {"lustre-latest.md": "lustre-summary.md", "lustre-latest.json": "lustre-summary.json",
                 "lustre-latest.html": "lustre-report.html"}
        files = [os.path.join(lc.results_dir, name) for name in names]
        files += [os.path.join(lc.results_dir, name) for name in summary.get("sources", [])]
        os.makedirs(os.path.dirname(bundle), exist_ok=True)
        with tarfile.open(bundle, "w:gz") as archive:
            for path in files:
                if os.path.exists(path):
                    archive.add(path, arcname=names.get(os.path.basename(path), os.path.basename(path)))
    with open(bundle, "rb") as handle:
        raw = handle.read()
    data = base64.b64encode(raw).decode("ascii")
    total = max(1, math.ceil(len(data) / args.chunk_size))
    if not 0 <= args.chunk < total:
        raise SystemExit(f"error: chunk {args.chunk} out of range (0-{total - 1})")
    print(f"LUSTREPERF-EXPORT chunk={args.chunk} total={total} sha256={hashlib.sha256(raw).hexdigest()}")
    print(data[args.chunk * args.chunk_size:(args.chunk + 1) * args.chunk_size])
    return 0


# --------------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lustreperf", description=__doc__.split("\n\n")[0], epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"lustreperf {VERSION}")
    parser.add_argument("--job-id", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(metavar="<command>")

    def add(name: str, func, text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=text, description=text)
        sub.set_defaults(func=func)
        return sub

    def workers(sub):
        sub.add_argument("--workers", type=bp.thread_count, default=64,
                         help="file operations in flight (1-256, processes x threads) (default: %(default)s)")

    def size(sub):
        sub.add_argument("--size", type=parse_bytes, default=64 * KIB, help="file size, e.g. 65536 or 64KiB (default: 64KiB)")

    def load(sub):
        sub.add_argument("--objects", type=bp.positive_int, default=1_000_000, help="dataset size (default: 1,000,000)")
        sub.add_argument("--batch", type=bp.positive_int, default=100_000, help="files per resumable batch (default: 100,000)")
        sub.add_argument("--force", action="store_true", help="recreate the dataset even if it exists")

    def latency(sub):
        sub.add_argument("--reads", type=bp.positive_int, default=10_000, help="measured random reads (default: 10,000)")
        sub.add_argument("--warmup", type=bp.non_negative_int, default=50, help="unmeasured reads first (default: 50)")
        sub.add_argument("--threads", type=bp.thread_count, default=1, help="concurrent readers (default: 1)")
        sub.add_argument("--seed", type=int, help="random seed for the file selection (default: random)")
        sub.add_argument("--first-read", type=parse_bytes, default=4 * KIB,
                         help="bytes read per file in the first-byte pass (as many other files as --reads); 0 skips "
                              "that pass (default: 4KiB)")

    def throughput(sub):
        sub.add_argument("--files", type=bp.positive_int, default=100_000, help="files to write/read/list (default: 100,000)")
        sub.add_argument("--iterations", type=bp.positive_int, default=3, help="repetitions (default: 3)")
        sub.add_argument("--keep", action="store_true", help="keep the written files (tp-* folders)")

    def bandwidth(sub, standalone: bool = False):
        sub.add_argument("--streams", type=bp.thread_count, default=16, help="parallel files (default: %(default)s)")
        sub.add_argument("--file-size", type=parse_bytes, default=4 * GIB, help="size of each file (default: 4GiB)")
        sub.add_argument("--block", type=parse_bytes, default=4 * MIB, help="I/O size (default: 4MiB)")
        sub.add_argument("--iterations" if standalone else "--bandwidth-iterations", dest="bw_iterations",
                         type=bp.positive_int, default=3, help="repetitions (default: 3)")

    sub = add("info", cmd_info, "show the file system, Lustre client, network and VM details")
    sub = add("mount", cmd_mount, "mount the file system (every test does this when needed)")
    sub = add("umount", cmd_umount, "unmount the file system (do this before deleting it)")
    sub = add("load", cmd_load, "create the latency dataset (1,000,000 x 64 KiB random files)")
    for option in (size, load, workers):
        option(sub)
    sub = add("latency", cmd_latency, "latency test: random reads from the dataset, cold cache")
    latency(sub)
    sub = add("throughput", cmd_throughput, "write, read, list and delete 100,000 x 64 KiB files")
    for option in (size, throughput, workers):
        option(sub)
    sub = add("bandwidth", cmd_bandwidth, "large-file sequential write and read with parallel streams")
    bandwidth(sub, standalone=True)
    sub = add("run-all", cmd_run_all, "load the dataset if needed, run every test and report")
    for option in (size, load, latency, throughput, bandwidth, workers):
        option(sub)
    sub.set_defaults(print="md", zones=False)
    sub = add("report", cmd_report, "summarize the latest results (lustre-latest.md/.json/.html in the results folder)")
    sub.add_argument("--zones", action="store_true",
                     help="compare availability zones: combine the results every zonal VM published to the results container")
    sub.add_argument("--since", help="with --zones: only runs started at or after this UTC time, e.g. 2026-11-18T12:00:00Z")
    sub.add_argument("--zone-map", help="logical=physical zone labels, e.g. 1=swedencentral-az2,2=swedencentral-az3")
    sub.add_argument("--print", choices=("md", "json", "none"), default="md", help="what to print (default: md)")
    sub = add("start", cmd_start, "run a command as a background job, e.g. 'lustreperf start run-all'")
    sub.add_argument("job_command", nargs=argparse.REMAINDER, metavar="<command> [options]")
    sub = add("status", cmd_status, "state of the background job and the end of its log")
    sub.add_argument("--lines", type=int, default=15, help="log lines to show (default: 15)")
    sub.add_argument("--width", type=int, default=200, help="truncate log lines to this width (default: 200)")
    sub = add("export", cmd_export, "print the latest report bundle as base64 chunks (used by scripts/demo.*)")
    sub.add_argument("--chunk", type=int, default=0)
    sub.add_argument("--chunk-size", type=bp.positive_int, default=2800)
    sub = add("cleanup", cmd_cleanup, "delete leftover test folders (tp-*, bw-*)")
    sub.add_argument("--dataset", action="store_true", help="also delete the latency dataset")
    workers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    lc = LustreConfig()
    try:
        exit_code = int(args.func(lc, args) or 0)
    except SystemExit as exc:
        if isinstance(exc.code, int) or exc.code is None:
            exit_code = exc.code or 0
        else:
            print(exc.code, file=sys.stderr)
            exit_code = 1
    except KeyboardInterrupt:
        exit_code = 130
    except Exception:  # noqa: BLE001 - report any failure of a background job in its log and state
        traceback.print_exc()
        exit_code = 1
    if args.job_id:
        finish_job(lc, args.job_id, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
