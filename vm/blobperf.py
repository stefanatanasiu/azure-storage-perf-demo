#!/usr/bin/env python3
"""blobperf - compare Azure Premium and Standard block blob storage performance.

Replicates the tests from the Azure blog post "Premium Block Blob Storage - a new level of
performance" (November 2018):
https://azure.microsoft.com/en-us/blog/premium-block-blob-storage-a-new-level-of-performance/

  latency     Read a random selection of 10,000 objects from a population of 1,000,000 64 KiB
              objects, one request at a time, measuring time to last byte.
  throughput  Upload and download 100,000 64 KiB objects with AzCopy v10 between a RAM disk and
              each account, then list the 100,000 objects.

Runs on an Azure Linux VM whose managed identity has "Storage Blob Data Contributor" on both
accounts (see infra/ in the repository). Needs only the Python 3.8+ standard library and AzCopy v10.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import datetime as dt
import email.utils
import gc
import glob
import hashlib
import html
import http.client
import ipaddress
import json
import math
import os
import platform
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
STORAGE_API_VERSION = "2024-11-04"
USER_AGENT = f"blobperf/{VERSION} python/{platform.python_version()}"
ENV_FILE = "/etc/blobperf/blobperf.env"
KIB = 1024
NAMING_SCHEME = "sha256-prefix4-index7-v1"
DATASET_MARKER = "_blobperf/dataset.json"
BATCH_MARKER = "_blobperf/load/batch-{:05d}.json"
NEXT_MARKER = re.compile(rb"<NextMarker>([^<]+)</NextMarker>")
CONTAINER_NAME = re.compile(rb"<Container>\s*<Name>([^<]+)</Name>")

# Results published in the blog post (Standard D32 v3 VM, November 2018).
ARTICLE = {
    "latency_ms": {"premium": {"avg": 5.3, "p99": 6.9}, "standard": {"avg": 61.4, "p99": 287.3}},
    "seconds": {
        "upload": {"premium": 20.5, "standard": 107.3},
        "download": {"premium": 24.1, "standard": 118.8},
        "list": {"premium": 10.7, "standard": 26.3},
    },
}
# Same colours as the blog post's chart.
COLORS = {"premium": "#4472C4", "standard": "#ED7D31"}
EXTRA_COLORS = ("#70AD47", "#7F7F7F", "#FFC000", "#5B9BD5")

EPILOG = """examples:
  blobperf info                      configuration, VM details, access to both accounts
  blobperf run-all                   load the dataset if needed, run every test, print the report
  blobperf start run-all             same, as a background job (survives SSH/run-command disconnects)
  blobperf status                    progress of the background job
  blobperf latency --reads 2000      quicker latency run
  blobperf throughput --iterations 1
  blobperf report                    summary of the latest results (markdown + report.html)
"""


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def log(message: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}Z] {message}", flush=True)


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    """Timestamp that is also valid inside container names (lowercase, no colons)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dt%H%M%Sz")


def parse_size(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(k|kb|kib|m|mb|mib)?\s*", str(text), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size {text!r} (examples: 65536, 64KiB, 1MiB)")
    factor = {"": 1, "k": KIB, "kb": KIB, "kib": KIB, "m": KIB * KIB, "mb": KIB * KIB, "mib": KIB * KIB}
    return int(match.group(1)) * factor[(match.group(2) or "").lower()]


def fmt_size(size: int) -> str:
    for unit, factor in (("MiB", KIB * KIB), ("KiB", KIB)):
        if size >= factor and size % factor == 0:
            return f"{size // factor} {unit}"
    return f"{size} B"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def fmt_num(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def percentile(ordered: list[float], pct: float) -> float | None:
    """Linear interpolation between closest ranks (same as numpy's default)."""
    if not ordered:
        return None
    rank = (len(ordered) - 1) * pct / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize(values: list[float]) -> dict:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return {"count": 0}
    mean = sum(ordered) / count
    stdev = math.sqrt(sum((v - mean) ** 2 for v in ordered) / (count - 1)) if count > 1 else 0.0
    stats = {"count": count, "avg": mean, "stdev": stdev, "min": ordered[0]}
    for label, pct in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99), ("p99_9", 99.9)):
        stats[label] = percentile(ordered, pct)
    stats["max"] = ordered[-1]
    return {key: round(value, 4) if isinstance(value, float) else value for key, value in stats.items()}


def seconds_stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"median": round(percentile(ordered, 50), 3), "min": round(ordered[0], 3),
            "max": round(ordered[-1], 3), "runs": [round(v, 3) for v in values]}


def object_name(index: int) -> str:
    """Deterministic name of object #index, e.g. '3fa9-0012345.bin'.

    The short hash prefix spreads consecutive objects over the whole key range (the partition
    naming guidance for Blob storage), so neither account is limited by a single hot partition
    while it ingests, and readers can derive any name from its index without listing.
    """
    return f"{hashlib.sha256(str(index).encode()).hexdigest()[:4]}-{index:07d}.bin"


def write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temporary, path)


def read_json_file(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Target:
    name: str      # "premium", "standard", ... (BLOBPERF_<NAME>_ENDPOINT in the env file)
    account: str
    host: str      # <account>.blob.core.windows.net

    def url(self, container: str, blob: str = "") -> str:
        return f"https://{self.host}/{container}/{blob}"


class Config:
    """Settings from /etc/blobperf/blobperf.env (written at deployment), overridable by BLOBPERF_* env vars."""

    def __init__(self) -> None:
        values: dict[str, str] = {}
        self.env_file = os.environ.get("BLOBPERF_ENV_FILE", ENV_FILE)
        if os.path.isfile(self.env_file):
            with open(self.env_file, encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        values[key.strip()] = value.strip().strip("'\"")
        values.update({k: v for k, v in os.environ.items() if k.startswith("BLOBPERF_")})
        self.values = values
        self.workdir = values.get("BLOBPERF_WORKDIR", "/ramdisk/blobperf")
        self.state_dir = values.get("BLOBPERF_STATE_DIR", "/var/lib/blobperf")
        self.log_dir = values.get("BLOBPERF_LOG_DIR", "/var/log/blobperf")
        self.results_dir = os.path.join(self.state_dir, "results")
        self.azcopy = values.get("BLOBPERF_AZCOPY") or shutil.which("azcopy") or "/usr/local/bin/azcopy"
        self.mde_exclude = values.get("BLOBPERF_MDE_EXCLUDE_WORKDIR", "true").strip().lower() in ("1", "true", "yes")
        self.msi_client_id = values.get("BLOBPERF_MSI_CLIENT_ID") or None
        self._token: ManagedIdentityToken | None = None

    def token(self) -> "ManagedIdentityToken":
        if self._token is None:
            self._token = ManagedIdentityToken(self.msi_client_id)
        return self._token

    def client(self, target: Target) -> "BlobHttp":
        return BlobHttp(target.host, self.token())

    def target(self, name: str) -> Target:
        key = name.upper()
        endpoint = self.values.get(f"BLOBPERF_{key}_ENDPOINT", "")
        account = self.values.get(f"BLOBPERF_{key}_ACCOUNT", "")
        if not endpoint and account:
            endpoint = f"https://{account}.blob.core.windows.net/"
        host = urllib.parse.urlparse(endpoint).hostname if endpoint else None
        if not host:
            raise SystemExit(f"error: no endpoint for target '{name}' (set BLOBPERF_{key}_ENDPOINT in {self.env_file})")
        return Target(name, account or host.split(".")[0], host)

    def targets(self, spec: str) -> list[Target]:
        names = [n.strip().lower() for n in spec.split(",") if n.strip()]
        if not names or len(set(names)) != len(names):
            raise SystemExit(f"error: --targets needs a comma separated list without duplicates, got {spec!r}")
        return [self.target(name) for name in names]


# --------------------------------------------------------------------------------------------
# Blob REST client (managed identity + keep-alive HTTPS, standard library only)
# --------------------------------------------------------------------------------------------

class ManagedIdentityToken:
    """OAuth token for https://storage.azure.com/ from the VM's managed identity (IMDS endpoint)."""

    def __init__(self, client_id: str | None = None) -> None:
        self._client_id = client_id
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 300:
                return self._token
            query = {"api-version": "2019-08-01", "resource": "https://storage.azure.com/"}
            if self._client_id:
                query["client_id"] = self._client_id
            request = urllib.request.Request(
                "http://169.254.169.254/metadata/identity/oauth2/token?" + urllib.parse.urlencode(query),
                headers={"Metadata": "true"})
            error: Exception | None = None
            for attempt in range(6):
                try:
                    with self._opener.open(request, timeout=15) as response:
                        payload = json.load(response)
                    self._token = payload["access_token"]
                    self._expires = float(payload.get("expires_on") or time.time() + 3600)
                    return self._token
                except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                    error = exc
                    time.sleep(min(2 ** attempt, 20))
            raise RuntimeError(f"cannot get a managed identity token from IMDS: {error}")


class BlobHttp:
    """One persistent HTTPS connection to a blob endpoint (not thread-safe: use one per thread).

    Deliberately minimal so that client overhead stays far below the storage latency being
    measured: the token and headers are prepared before the clock starts, and the timer covers
    only sending the request and receiving the complete response.
    """

    def __init__(self, host: str, token: ManagedIdentityToken, timeout: float = 60) -> None:
        self.host = host
        self._token = token
        self._timeout = timeout
        self._context = ssl.create_default_context()
        self._conn: http.client.HTTPSConnection | None = None
        self.connects = 0

    def _connect(self) -> None:
        self.close()
        conn = http.client.HTTPSConnection(self.host, 443, timeout=self._timeout, context=self._context)
        conn.connect()
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._conn = conn
        self.connects += 1

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None) -> tuple[int, dict, bytes, float, float]:
        """Returns (status, headers, body, seconds to first byte, seconds to last byte).

        A dropped keep-alive connection is re-opened and the request retried once.
        """
        for attempt in (1, 2):
            if self._conn is None:
                self._connect()
            request_headers = {
                "Authorization": "Bearer " + self._token.get(),
                "x-ms-version": STORAGE_API_VERSION,
                "x-ms-date": email.utils.formatdate(usegmt=True),
                "User-Agent": USER_AGENT,
            }
            if method in ("PUT", "POST", "DELETE"):
                request_headers["Content-Length"] = str(len(body) if body else 0)
            if headers:
                request_headers.update(headers)
            try:
                start = time.perf_counter()
                self._conn.request(method, path, body=body, headers=request_headers)
                response = self._conn.getresponse()
                first_byte = time.perf_counter()
                data = response.read()
                last_byte = time.perf_counter()
            except (http.client.HTTPException, OSError):
                self.close()
                if attempt == 2:
                    raise
                continue
            if response.will_close:
                self.close()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, data, first_byte - start, last_byte - start
        raise AssertionError("unreachable")


class StorageError(RuntimeError):
    def __init__(self, what: str, status: int, headers: dict, body: bytes) -> None:
        self.status = status
        self.code = headers.get("x-ms-error-code", "")
        detail = body[:300].decode("utf-8", "replace").strip() if body else ""
        super().__init__(f"{what}: HTTP {status} {self.code} {detail}".strip())


def blob_path(container: str, name: str) -> str:
    return f"/{container}/{urllib.parse.quote(name)}"


def call(client: BlobHttp, method: str, path: str, expected: tuple, what: str,
         body: bytes | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
    status, response_headers, data, _, _ = client.request(method, path, body=body, headers=headers)
    if status not in expected:
        raise StorageError(what, status, response_headers, data)
    return status, response_headers, data


def create_container(client: BlobHttp, container: str, timeout: float = 180) -> None:
    deadline = time.time() + timeout
    while True:
        status, headers, data, _, _ = client.request("PUT", f"/{container}?restype=container")
        code = headers.get("x-ms-error-code", "")
        if status == 201 or (status == 409 and code == "ContainerAlreadyExists"):
            return
        if status == 409 and code == "ContainerBeingDeleted" and time.time() < deadline:
            time.sleep(10)
            continue
        raise StorageError(f"create container {container}", status, headers, data)


def delete_container(client: BlobHttp, container: str) -> None:
    call(client, "DELETE", f"/{container}?restype=container", (202, 404), f"delete container {container}")


def put_json_blob(client: BlobHttp, container: str, name: str, payload: dict) -> None:
    call(client, "PUT", blob_path(container, name), (201,), f"write {container}/{name}",
         body=json.dumps(payload, indent=2).encode(),
         headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json"})


def get_json_blob(client: BlobHttp, container: str, name: str) -> dict | None:
    status, headers, data, _, _ = client.request("GET", blob_path(container, name))
    if status == 404:
        return None
    if status != 200:
        raise StorageError(f"read {container}/{name}", status, headers, data)
    return json.loads(data)


def list_blobs(client: BlobHttp, container: str, prefix: str | None = None, page_size: int = 5000) -> dict:
    """Enumerate a container with List Blobs (flat, 5,000 per page) and time it.

    Blobs are counted from the raw XML rather than fully parsed, so the timing reflects the
    service, not client-side XML processing.
    """
    marker, count, pages, page_ms = "", 0, 0, []
    start = time.perf_counter()
    while True:
        query = {"restype": "container", "comp": "list", "maxresults": str(page_size)}
        if prefix:
            query["prefix"] = prefix
        if marker:
            query["marker"] = marker
        status, headers, data, _, seconds = client.request("GET", f"/{container}?{urllib.parse.urlencode(query)}")
        if status != 200:
            raise StorageError(f"list {container}", status, headers, data)
        count += data.count(b"<Blob>")
        pages += 1
        page_ms.append(seconds * 1000)
        match = NEXT_MARKER.search(data)
        marker = html.unescape(match.group(1).decode()) if match else ""
        if not marker:
            break
    elapsed = time.perf_counter() - start
    return {"count": count, "pages": pages, "seconds": round(elapsed, 3), "page_ms": summarize(page_ms)}


def list_containers(client: BlobHttp, prefix: str) -> list[str]:
    names, marker = [], ""
    while True:
        query = {"comp": "list", "prefix": prefix, "maxresults": "5000"}
        if marker:
            query["marker"] = marker
        _, _, data = call(client, "GET", "/?" + urllib.parse.urlencode(query), (200,), "list containers")
        names += [html.unescape(name.decode()) for name in CONTAINER_NAME.findall(data)]
        match = NEXT_MARKER.search(data)
        marker = html.unescape(match.group(1).decode()) if match else ""
        if not marker:
            return names


def access_hint(code: str) -> str:
    if code == "AuthorizationPermissionMismatch":
        return "the VM identity lacks Storage Blob Data Contributor (new role assignments take a few minutes)"
    if code == "AuthorizationFailure":
        return ("the storage firewall rejected the VM's network (new private endpoints and network rules take a few "
                "minutes); if a policy disables public network access, deploy with storageNetworkMode=PrivateEndpoint")
    return ""


def network_path(ip: str) -> str:
    return "private endpoint" if ipaddress.ip_address(ip).is_private else "public endpoint"


def wait_for_access(cfg: Config, target: Target, container: str | None = None, timeout: float = 900) -> None:
    """Block until the VM identity can use the account; creates `container` if it is missing.

    New role assignments, private endpoints and network rules can take several minutes to apply,
    which shows up as HTTP 403 in the meantime.
    """
    client = cfg.client(target)
    deadline, announced = time.time() + timeout, False
    try:
        while True:
            path = f"/{container}?restype=container" if container else "/?comp=list&maxresults=1"
            status, headers, data, _, _ = client.request("GET", path)
            if status == 200:
                return
            if status == 404 and container:
                create_container(client, container)
                continue
            if status == 403 and time.time() < deadline:
                if not announced:
                    code = headers.get("x-ms-error-code", "")
                    log(f"{target.name}: waiting for data-plane access, HTTP 403 {code}: {access_hint(code)}")
                    announced = True
                time.sleep(15)
                continue
            raise StorageError(f"{target.name}: access check", status, headers, data)
    finally:
        client.close()


def tcp_connect_ms(host: str, samples: int = 20) -> dict:
    """Median TCP handshake time to the blob endpoint: roughly one network round trip."""
    address = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    times = []
    for _ in range(samples):
        start = time.perf_counter()
        with socket.create_connection((address, 443), timeout=5):
            times.append((time.perf_counter() - start) * 1000)
        time.sleep(0.02)
    ordered = sorted(times)
    return {"ip": address, "median": round(percentile(ordered, 50), 3), "min": round(ordered[0], 3)}


# --------------------------------------------------------------------------------------------
# VM environment
# --------------------------------------------------------------------------------------------

def imds_instance() -> dict:
    request = urllib.request.Request("http://169.254.169.254/metadata/instance?api-version=2021-02-01",
                                     headers={"Metadata": "true"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            compute = json.load(response).get("compute", {})
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    image = compute.get("storageProfile", {}).get("imageReference", {})
    return {
        "vm_name": compute.get("name"),
        "location": compute.get("location"),
        "zone": compute.get("zone") or None,
        "vm_size": compute.get("vmSize"),
        "image": ":".join(image.get(k) or "" for k in ("publisher", "offer", "sku", "version")).strip(":") or None,
    }


def nic_drivers() -> list[str]:
    drivers = set()
    try:
        for device in os.listdir("/sys/class/net"):
            link = f"/sys/class/net/{device}/device/driver"
            if os.path.islink(link):
                drivers.add(os.path.basename(os.readlink(link)))
    except OSError:
        pass
    return sorted(drivers)


def run_quiet(command: list[str], timeout: float = 60) -> str:
    try:
        return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"


def azcopy_version(cfg: Config) -> str | None:
    if not os.path.exists(cfg.azcopy):
        return None
    match = re.search(r"version\s+(\S+)", run_quiet([cfg.azcopy, "--version"]))
    return match.group(1) if match else None


def mde_info() -> dict:
    mdatp = shutil.which("mdatp")
    if not mdatp:
        return {"installed": False}
    return {"installed": True,
            "version": run_quiet([mdatp, "health", "--field", "app_version"]).strip('"'),
            "real_time_protection": run_quiet([mdatp, "health", "--field", "real_time_protection_enabled"])}


def memory_gib() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / KIB / KIB, 1)
    except OSError:
        pass
    return None


def environment_info(cfg: Config) -> dict:
    drivers = nic_drivers()
    info = {
        "tool_version": VERSION,
        "python": platform.python_version(),
        "kernel": platform.release(),
        "cpus": os.cpu_count(),
        "memory_gib": memory_gib(),
        "accelerated_networking": any(d == "mana" or d.startswith("mlx") for d in drivers),
        "nic_drivers": drivers,
        "azcopy": azcopy_version(cfg),
        "azcopy_concurrency": os.environ.get("AZCOPY_CONCURRENCY_VALUE", "default (16 x vCPU)"),
        "mde": mde_info(),
    }
    info.update(imds_instance())
    return info


def ensure_mde_exclusion(cfg: Config) -> str:
    """Exclude the RAM-disk work folder (random test data only) from Defender for Endpoint
    real-time scanning, so the AzCopy tests measure storage rather than antivirus overhead."""
    mdatp = shutil.which("mdatp")
    if not mdatp:
        return "MDE not installed"
    if not cfg.mde_exclude:
        return "MDE installed, work folder exclusion disabled by configuration"
    folder = cfg.workdir.rstrip("/") + "/"
    if folder in run_quiet([mdatp, "exclusion", "list"]):
        return f"MDE installed, {folder} excluded"
    output = run_quiet([mdatp, "exclusion", "folder", "add", "--path", folder])
    if folder in run_quiet([mdatp, "exclusion", "list"]):
        return f"MDE installed, {folder} excluded (added)"
    return f"MDE installed, could not exclude {folder}: {output[:160]}"


def prepare(cfg: Config, need_azcopy: bool, args: argparse.Namespace | None = None) -> dict:
    if args is not None and getattr(args, "azcopy_concurrency", None):
        os.environ["AZCOPY_CONCURRENCY_VALUE"] = str(args.azcopy_concurrency)
    os.makedirs(cfg.workdir, exist_ok=True)
    os.makedirs(cfg.results_dir, exist_ok=True)
    if need_azcopy and not os.path.exists(cfg.azcopy):
        raise SystemExit(f"error: AzCopy not found at {cfg.azcopy} (set BLOBPERF_AZCOPY)")
    env = environment_info(cfg)
    env["mde_status"] = ensure_mde_exclusion(cfg)
    log(f"{env.get('vm_size')} in {env.get('location')} | {env['cpus']} vCPU | "
        f"accelerated networking: {env['accelerated_networking']} | AzCopy {env['azcopy']} | {env['mde_status']}")
    return env


# --------------------------------------------------------------------------------------------
# data generation and AzCopy
# --------------------------------------------------------------------------------------------

def _write_random_files(job: tuple) -> int:
    directory, indices, size = job
    for index in indices:
        with open(os.path.join(directory, object_name(index)), "wb") as handle:
            handle.write(os.urandom(size))
    return len(indices)


def generate_files(directory: str, first: int, count: int, size: int) -> float:
    """Write `count` files of `size` cryptographically random bytes; returns seconds taken."""
    os.makedirs(directory, exist_ok=True)
    workers = max(1, min(os.cpu_count() or 1, count))
    indices = range(first, first + count)
    jobs = [(directory, indices[w::workers], size) for w in range(workers)]
    start = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        written = sum(pool.map(_write_random_files, jobs))
    if written != count:
        raise RuntimeError(f"generated {written} files, expected {count}")
    return time.perf_counter() - start


AZCOPY_SUMMARY_KEYS = ("JobStatus", "TotalTransfers", "TransfersCompleted", "TransfersFailed", "TransfersSkipped",
                       "TotalBytesTransferred", "AverageIOPS", "AverageE2EMilliseconds", "ServerBusyPercentage",
                       "NetworkErrorPercentage", "PerfConstraint")


def parse_azcopy_summary(stdout: str) -> dict:
    """Extract the end-of-job summary from AzCopy's `--output-type=json` output."""
    summary: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("MessageType") != "EndOfJob":
            continue
        content = message.get("MessageContent")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = {}
        if isinstance(content, dict):
            summary = content
    return {key: summary[key] for key in AZCOPY_SUMMARY_KEYS if key in summary}


def run_azcopy(cfg: Config, arguments: list[str], label: str, expected: int = 0) -> dict:
    """Run one AzCopy job (managed identity auth) and time it with a wall clock.

    Job plans and logs go to the RAM disk so local disk speed does not affect the result. AzCopy's
    JSON progress messages (every ~2 s) are timestamped on arrival to show where the time went:
    seconds until 50% and 99% of `expected` transfers had completed, versus the end of the job.
    """
    jobdir = os.path.join(cfg.workdir, "azcopy", f"{label}-{time.time_ns()}")
    env = dict(os.environ)
    env.setdefault("AZCOPY_AUTO_LOGIN_TYPE", "MSI")
    if cfg.msi_client_id:
        env.setdefault("AZCOPY_MSI_CLIENT_ID", cfg.msi_client_id)
    env["AZCOPY_LOG_LOCATION"] = os.path.join(jobdir, "logs")
    env["AZCOPY_JOB_PLAN_LOCATION"] = os.path.join(jobdir, "plans")
    env.setdefault("AZCOPY_DISABLE_SYSLOG", "true")
    command = [cfg.azcopy, *arguments, "--output-type=json", "--log-level=ERROR"]
    lines, other, milestones = [], [], {}
    start = time.perf_counter()
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in process.stdout:
        now = time.perf_counter() - start
        lines.append(line)
        if not line.startswith("{"):
            other.append(line)
            continue
        if expected and '"Progress"' in line:
            try:
                done = int(json.loads(json.loads(line)["MessageContent"]).get("TransfersCompleted") or 0)
            except (ValueError, KeyError, TypeError):
                continue
            for key, share in (("half_s", 0.5), ("p99_s", 0.99)):
                if key not in milestones and done >= expected * share:
                    milestones[key] = round(now, 1)
    process.wait()
    seconds = time.perf_counter() - start
    result = {"seconds": round(seconds, 3), "exit_code": process.returncode,
              "summary": parse_azcopy_summary("".join(lines))}
    if milestones:
        result["progress"] = milestones
    if process.returncode != 0:
        kept = os.path.join(cfg.state_dir, "azcopy-failures", os.path.basename(jobdir))
        try:
            shutil.copytree(jobdir, kept)
            result["logs"] = kept
        except OSError:
            pass
        result["stderr"] = "".join(other or lines)[-1000:]
    shutil.rmtree(jobdir, ignore_errors=True)
    return result


def check_transfers(result: dict, expected: int, what: str) -> None:
    summary = result["summary"]
    completed = int(summary.get("TransfersCompleted") or 0)
    failed = int(summary.get("TransfersFailed") or 0)
    if result["exit_code"] == 0 and completed == expected and not failed:
        return
    message = (f"{what}: AzCopy exit code {result['exit_code']}, {completed:,}/{expected:,} transfers "
               f"completed, {failed:,} failed")
    if result.get("logs"):
        message += f"; logs in {result['logs']}"
    detail = (result.get("stderr") or "").strip().splitlines()
    if detail:
        message += "; " + detail[-1][:300]
    raise RuntimeError(message)


# --------------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------------

def cmd_info(cfg: Config, args: argparse.Namespace) -> int:
    env = environment_info(cfg)
    print(f"blobperf {VERSION} (config {cfg.env_file})")
    for key in ("vm_name", "location", "zone", "vm_size", "cpus", "memory_gib", "image", "kernel", "python",
                "accelerated_networking", "nic_drivers", "azcopy", "mde"):
        print(f"  {key:23} {env.get(key)}")
    try:
        usage = shutil.disk_usage(os.path.dirname(cfg.workdir.rstrip("/")) or "/")
        print(f"  {'ramdisk':23} {usage.free / KIB ** 3:.1f} GiB free of {usage.total / KIB ** 3:.1f} GiB")
    except OSError:
        print(f"  {'ramdisk':23} not mounted")
    for target in cfg.targets(args.targets):
        client = cfg.client(target)
        try:
            rtt = tcp_connect_ms(target.host, samples=5)
            status, headers, _, _, _ = client.request("GET", "/?comp=list&maxresults=1")
            code = headers.get("x-ms-error-code", "")
            access = "ok" if status == 200 else f"HTTP {status} {code}"
            marker = get_json_blob(client, "latency", DATASET_MARKER) if status == 200 else None
            dataset = f"{marker['objects']:,} x {fmt_size(marker['size'])}" if marker else "not loaded"
            print(f"  {target.name:9} {target.account:24} {rtt['ip']} ({network_path(rtt['ip'])}) tcp {rtt['median']:.2f} ms"
                  f" | access {access} | dataset {dataset}")
            if status != 200 and access_hint(code):
                print(f"            hint: {access_hint(code)}")
        except (OSError, http.client.HTTPException, RuntimeError, ValueError) as exc:
            print(f"  {target.name:9} {target.account:24} error: {exc}")
        finally:
            client.close()
    return 0


def cmd_load(cfg: Config, args: argparse.Namespace) -> int:
    """Upload the latency dataset: `objects` random objects of `size` bytes, in batches via AzCopy."""
    targets = cfg.targets(args.targets)
    env = prepare(cfg, need_azcopy=True, args=args)
    for target in targets:
        wait_for_access(cfg, target, args.container)
    clients = {t.name: cfg.client(t) for t in targets}
    expected = {"objects": args.objects, "size": args.size, "naming": NAMING_SCHEME}
    pending = []
    for target in targets:
        marker = get_json_blob(clients[target.name], args.container, DATASET_MARKER)
        if marker and all(marker.get(k) == v for k, v in expected.items()) and not args.force:
            log(f"{target.name}: dataset already loaded ({marker['objects']:,} x {fmt_size(marker['size'])}), skipping")
        else:
            pending.append(target)
    if not pending:
        return 0
    batches = math.ceil(args.objects / args.batch)
    batch_dir = os.path.join(cfg.workdir, "load")
    log(f"loading {args.objects:,} x {fmt_size(args.size)} random objects into "
        f"{', '.join(t.name for t in pending)} ({batches} batches of up to {args.batch:,})")
    started = time.perf_counter()
    for batch in range(batches):
        first = batch * args.batch
        count = min(args.batch, args.objects - first)
        todo = []
        for target in pending:
            done = None if args.force else get_json_blob(clients[target.name], args.container, BATCH_MARKER.format(batch))
            if not (done and done.get("first") == first and done.get("count") == count and done.get("size") == args.size
                    and done.get("naming") == NAMING_SCHEME):
                todo.append(target)
        if not todo:
            log(f"batch {batch + 1}/{batches}: already loaded")
            continue
        shutil.rmtree(batch_dir, ignore_errors=True)
        generate_files(batch_dir, first, count, args.size)
        source = os.path.join(batch_dir, "*")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(todo)) as pool:
            futures = {t.name: pool.submit(run_azcopy, cfg, ["copy", source, t.url(args.container), "--overwrite=true"],
                                           f"load-{t.name}", count) for t in todo}
            results = {name: future.result() for name, future in futures.items()}
        for target in todo:
            check_transfers(results[target.name], count, f"{target.name} batch {batch + 1}")
            put_json_blob(clients[target.name], args.container, BATCH_MARKER.format(batch),
                          {"first": first, "count": count, "size": args.size, "naming": NAMING_SCHEME, "loaded_utc": utc_iso()})
        rates = " | ".join(f"{name}: {r['seconds']:.1f} s ({count / r['seconds']:,.0f} obj/s)" for name, r in results.items())
        elapsed = time.perf_counter() - started
        eta = elapsed / (batch + 1) * (batches - batch - 1)
        log(f"batch {batch + 1}/{batches} ({first:,}-{first + count - 1:,}) {rates} | elapsed {fmt_duration(elapsed)}, "
            f"eta {fmt_duration(eta)}")
    shutil.rmtree(batch_dir, ignore_errors=True)
    for target in pending:
        put_json_blob(clients[target.name], args.container, DATASET_MARKER,
                      dict(expected, loaded_utc=utc_iso(), tool_version=VERSION, vm_size=env.get("vm_size")))
        clients[target.name].close()
    log(f"dataset loaded in {fmt_duration(time.perf_counter() - started)}")
    return 0


def measure_latency(cfg: Config, target: Target, container: str, warmup: list[int], measured: list[int],
                    expected_size: int, threads: int) -> dict:
    total = len(measured)
    ttfb, ttlb = [0.0] * total, [0.0] * total
    status, request_ids = [0] * total, [""] * total
    errors: list[str] = []
    failures: list[BaseException] = []
    spans: list[tuple[float, float]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)
    paths = [blob_path(container, object_name(i)) for i in measured]

    def worker(slot: int) -> None:
        client = None
        try:
            client = cfg.client(target)
            for index in warmup[slot::threads] or warmup[:1]:
                client.request("GET", blob_path(container, object_name(index)))
            barrier.wait()
            begin = time.perf_counter()
            for k in range(slot, total, threads):
                try:
                    code, headers, data, first, last = client.request("GET", paths[k])
                except Exception as exc:  # noqa: BLE001 - count any failed read as an error and keep going
                    status[k] = -1
                    with lock:
                        errors.append(f"{measured[k]}: {type(exc).__name__}: {exc}")
                    continue
                status[k], ttfb[k], ttlb[k] = code, first, last
                request_ids[k] = headers.get("x-ms-request-id", "")
                if code != 200 or len(data) != expected_size:
                    status[k] = code if code != 200 else -2
                    with lock:
                        errors.append(f"{measured[k]}: HTTP {code}, {len(data)} bytes")
            with lock:
                spans.append((begin, time.perf_counter()))
        except Exception as exc:  # noqa: BLE001 - surface failures from worker threads
            barrier.abort()
            with lock:
                failures.append(exc)
        finally:
            if client is not None:
                client.close()

    rtt = tcp_connect_ms(target.host)
    gc.collect()
    gc.disable()
    try:
        if threads == 1:
            worker(0)
        else:
            pool: list[threading.Thread] = []
            try:
                for slot in range(threads):
                    thread = threading.Thread(target=worker, args=(slot,))
                    thread.start()
                    pool.append(thread)
            except Exception as exc:  # noqa: BLE001 - e.g. the OS refuses more threads
                barrier.abort()
                failures.append(exc)
            for thread in pool:
                thread.join()
    finally:
        gc.enable()
    real = [f for f in failures if not isinstance(f, threading.BrokenBarrierError)]
    if failures:
        raise RuntimeError(f"{target.name}: latency test failed: {(real or failures)[0]!r}")
    wall = max(end for _, end in spans) - min(begin for begin, _ in spans)
    ok = [k for k in range(total) if status[k] == 200]
    slowest = sorted(ok, key=lambda k: ttlb[k], reverse=True)[:5]
    return {
        "account": target.account,
        "host": target.host,
        "ip": rtt["ip"],
        "network_path": network_path(rtt["ip"]),
        "tcp_connect_ms": rtt,
        "reads": total,
        "ok": len(ok),
        "errors": total - len(ok),
        "error_samples": errors[:10],
        "duration_s": round(wall, 3),
        "requests_per_s": round(len(ok) / wall, 1) if wall else None,
        "ttlb_ms": summarize([ttlb[k] * 1000 for k in ok]),
        "ttfb_ms": summarize([ttfb[k] * 1000 for k in ok]),
        "slowest": [{"object": measured[k], "ttlb_ms": round(ttlb[k] * 1000, 3), "request_id": request_ids[k]} for k in slowest],
        "_samples": list(zip(measured, status, ttfb, ttlb, request_ids)),
    }


def cmd_latency(cfg: Config, args: argparse.Namespace) -> int:
    targets = cfg.targets(args.targets)
    env = prepare(cfg, need_azcopy=False)
    population, size = None, None
    for target in targets:
        wait_for_access(cfg, target, args.container)
        client = cfg.client(target)
        marker = get_json_blob(client, args.container, DATASET_MARKER)
        client.close()
        if not marker:
            raise SystemExit(f"error: no dataset in {target.name}/{args.container}; run 'blobperf load' first")
        if marker.get("naming") != NAMING_SCHEME:
            raise SystemExit(f"error: dataset in {target.name} uses naming {marker.get('naming')!r}; reload with 'blobperf load --force'")
        if size is not None and marker["size"] != size:
            raise SystemExit("error: the accounts hold datasets with different object sizes; reload with 'blobperf load --force'")
        population = marker["objects"] if population is None else min(population, marker["objects"])
        size = marker["size"]
    if args.reads + args.warmup > population:
        raise SystemExit(f"error: --reads + --warmup ({args.reads + args.warmup:,}) exceeds the population ({population:,})")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2 ** 31)
    picks = random.Random(seed).sample(range(population), args.reads + args.warmup)
    warmup, measured = picks[:args.warmup], picks[args.warmup:]
    stamp = utc_stamp()
    result = {"type": "latency", "tool_version": VERSION, "started_utc": utc_iso(), "environment": env,
              "dataset": {"container": args.container, "objects": population, "object_size": size, "naming": NAMING_SCHEME},
              "reads": args.reads, "warmup": args.warmup, "threads": args.threads, "seed": seed, "targets": {}}
    for target in targets:
        log(f"{target.name}: {args.reads:,} random reads of {fmt_size(size)} from {population:,} objects "
            f"({args.threads} thread{'s' if args.threads > 1 else ''}, seed {seed})")
        measurement = measure_latency(cfg, target, args.container, warmup, measured, size, args.threads)
        samples = measurement.pop("_samples")
        csv_path = os.path.join(cfg.results_dir, f"latency-{stamp}-{target.name}.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            handle.write("object,status,ttfb_ms,ttlb_ms,request_id\n")
            for obj, code, first, last, request_id in samples:
                handle.write(f"{obj},{code},{first * 1000:.3f},{last * 1000:.3f},{request_id}\n")
        measurement["samples_csv"] = os.path.basename(csv_path)
        result["targets"][target.name] = measurement
        stats = measurement["ttlb_ms"]
        log(f"{target.name}: avg {fmt_num(stats.get('avg'))} ms | p50 {fmt_num(stats.get('p50'))} | "
            f"p99 {fmt_num(stats.get('p99'))} | p99.9 {fmt_num(stats.get('p99_9'))} | max {fmt_num(stats.get('max'))} ms | "
            f"tcp rtt {measurement['tcp_connect_ms']['median']:.2f} ms | errors {measurement['errors']}")
    result["finished_utc"] = utc_iso()
    path = os.path.join(cfg.results_dir, f"latency-{stamp}.json")
    write_json_atomic(path, result)
    log(f"results: {path}")
    return 0


def cmd_throughput(cfg: Config, args: argparse.Namespace) -> int:
    """AzCopy upload + download of `files` x `size` between the RAM disk and each account, then list them."""
    targets = cfg.targets(args.targets)
    env = prepare(cfg, need_azcopy=True, args=args)
    for target in targets:
        wait_for_access(cfg, target)
    base = os.path.join(cfg.workdir, "throughput")
    source, download = os.path.join(base, "upload"), os.path.join(base, "download")
    shutil.rmtree(base, ignore_errors=True)
    log(f"generating {args.files:,} random files of {fmt_size(args.size)} in {source}")
    generated = generate_files(source, 0, args.files, args.size)
    stamp = utc_stamp()
    result = {"type": "throughput", "tool_version": VERSION, "started_utc": utc_iso(), "environment": env,
              "files": args.files, "file_size": args.size, "iterations": args.iterations,
              "generate_seconds": round(generated, 3), "runs": [], "targets": {}}
    clients = {t.name: cfg.client(t) for t in targets}
    try:
        for iteration in range(1, args.iterations + 1):
            # Alternate the order so neither account always runs first.
            order = targets if iteration % 2 else list(reversed(targets))
            for target in order:
                client = clients[target.name]
                container = f"tp-{stamp}-{iteration}"
                create_container(client, container)
                upload = run_azcopy(cfg, ["copy", os.path.join(source, "*"), target.url(container), "--overwrite=true"],
                                    f"upload-{target.name}", args.files)
                check_transfers(upload, args.files, f"{target.name} upload")
                shutil.rmtree(download, ignore_errors=True)
                os.makedirs(download)
                down = run_azcopy(cfg, ["copy", target.url(container, "*"), download, "--recursive=true",
                                        "--overwrite=true"], f"download-{target.name}", args.files)
                check_transfers(down, args.files, f"{target.name} download")
                local_files = len(os.listdir(download))
                if local_files != args.files:
                    raise RuntimeError(f"{target.name} download: {local_files:,} files on disk, expected {args.files:,}")
                listing = list_blobs(client, container)
                if listing["count"] != args.files:
                    raise RuntimeError(f"{target.name} list: {listing['count']:,} blobs, expected {args.files:,}")
                if not args.keep:
                    delete_container(client, container)
                result["runs"].append({"iteration": iteration, "target": target.name, "container": container,
                                       "upload": upload, "download": down, "list": listing})

                def rate(job: dict) -> str:
                    tail = f", 99% at {job['progress']['p99_s']:.1f} s" if "p99_s" in job.get("progress", {}) else ""
                    return f"{job['seconds']:.1f} s ({args.files / job['seconds']:,.0f} files/s{tail})"

                log(f"{target.name} #{iteration}: upload {rate(upload)} | download {rate(down)} | "
                    f"list {listing['seconds']:.2f} s ({listing['pages']} pages)")
    finally:
        for client in clients.values():
            client.close()
        shutil.rmtree(base, ignore_errors=True)
    for target in targets:
        runs = [r for r in result["runs"] if r["target"] == target.name]
        result["targets"][target.name] = {
            "account": target.account,
            "upload_s": seconds_stats([r["upload"]["seconds"] for r in runs]),
            "download_s": seconds_stats([r["download"]["seconds"] for r in runs]),
            "list_s": seconds_stats([r["list"]["seconds"] for r in runs]),
        }
    result["finished_utc"] = utc_iso()
    path = os.path.join(cfg.results_dir, f"throughput-{stamp}.json")
    write_json_atomic(path, result)
    log(f"results: {path}")
    return 0


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    for target in cfg.targets(args.targets):
        client = cfg.client(target)
        try:
            listing = list_blobs(client, args.container, args.prefix)
        finally:
            client.close()
        log(f"{target.name}: {listing['count']:,} blobs in {listing['pages']} pages, {listing['seconds']:.2f} s "
            f"(page p50 {fmt_num(listing['page_ms'].get('p50'))} ms)")
    return 0


def cmd_run_all(cfg: Config, args: argparse.Namespace) -> int:
    started = time.perf_counter()
    steps = (("latency dataset", cmd_load), ("latency test", cmd_latency),
             ("throughput (AzCopy upload/download) and list test", cmd_throughput), ("report", cmd_report))
    for number, (title, command) in enumerate(steps, 1):
        log(f"=== step {number}/{len(steps)}: {title} ===")
        command(cfg, args)
    log(f"=== all done in {fmt_duration(time.perf_counter() - started)} ===")
    return 0


def cmd_cleanup(cfg: Config, args: argparse.Namespace) -> int:
    for target in cfg.targets(args.targets):
        client = cfg.client(target)
        try:
            names = list_containers(client, "tp-")
            if args.dataset:
                names.append("latency")
            for name in names:
                delete_container(client, name)
            log(f"{target.name}: deleted {len(names)} container(s){': ' + ', '.join(names) if names else ''}")
        finally:
            client.close()
    return 0


# --------------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------------

def latest_result(cfg: Config, kind: str) -> str | None:
    files = sorted(glob.glob(os.path.join(cfg.results_dir, f"{kind}-*[0-9]z.json")))
    return files[-1] if files else None


def build_summary(latency: dict | None, throughput: dict | None) -> dict:
    latency_env = (latency or {}).get("environment") or {}
    throughput_env = (throughput or {}).get("environment") or {}
    env = dict(latency_env or throughput_env)
    if throughput_env:
        # AzCopy only matters for the throughput test, and the tests may have run at different times.
        env["azcopy"] = throughput_env.get("azcopy")
        env["azcopy_concurrency"] = throughput_env.get("azcopy_concurrency")
        if latency_env and latency_env.get("mde_status") != throughput_env.get("mde_status"):
            env["mde_status"] = (f"latency test: {latency_env.get('mde_status')}; "
                                 f"throughput test: {throughput_env.get('mde_status')}")
    summary = {
        "generated_utc": utc_iso(),
        "tool_version": VERSION,
        "environment": {key: env.get(key) for key in (
            "location", "zone", "vm_size", "cpus", "memory_gib", "image", "kernel", "accelerated_networking",
            "nic_drivers", "azcopy", "azcopy_concurrency", "mde_status")},
        "article": ARTICLE,
    }
    if latency:
        summary["latency"] = {
            "started_utc": latency["started_utc"], "objects": latency["dataset"]["objects"],
            "object_size": latency["dataset"]["object_size"], "reads": latency["reads"],
            "threads": latency["threads"], "seed": latency["seed"],
            "targets": {name: {"account": r["account"], "ip": r.get("ip"), "network_path": r.get("network_path"),
                               "tcp_connect_ms": r["tcp_connect_ms"]["median"], "errors": r["errors"],
                               "requests_per_s": r["requests_per_s"], "ttlb_ms": r["ttlb_ms"],
                               "ttfb_ms": r["ttfb_ms"], "slowest": r.get("slowest", [])}
                        for name, r in latency["targets"].items()},
        }
    if throughput:
        summary["throughput"] = {
            "started_utc": throughput["started_utc"], "files": throughput["files"],
            "file_size": throughput["file_size"], "iterations": throughput["iterations"],
            "targets": throughput["targets"],
        }
    return summary


def _ratio(values: dict, slow: str = "standard", fast: str = "premium") -> str:
    if values.get(slow) and values.get(fast):
        return f"{values[slow] / values[fast]:.1f}x"
    return "-"


def latency_comparable(latency: dict) -> bool:
    """True when the latency run used the blog post's parameters."""
    return (latency["objects"], latency["reads"], latency["object_size"], latency["threads"]) == (1_000_000, 10_000, 64 * KIB, 1)


def throughput_comparable(throughput: dict) -> bool:
    """True when the throughput run used the blog post's parameters."""
    return (throughput["files"], throughput["file_size"]) == (100_000, 64 * KIB)


def _table_rows(summary: dict) -> list[tuple[str, list[tuple[str, dict, dict, bool]], str]]:
    """[(section title, [(row label, {target: value}, {premium/standard: blog value}, show ratio)], note)]

    Blog values are only shown when the run used the blog post's parameters, so that every
    comparison is like for like.
    """
    sections = []
    latency = summary.get("latency")
    if latency:
        targets = latency["targets"]
        comparable = latency_comparable(latency)
        rows = []
        for label, key, article_key in (("Average", "avg", "avg"), ("P50", "p50", None), ("P90", "p90", None),
                                        ("P99", "p99", "p99"), ("P99.9", "p99_9", None), ("Max", "max", None)):
            article = ({n: ARTICLE["latency_ms"][n][article_key] for n in ("premium", "standard")}
                       if article_key and comparable else {})
            rows.append((label, {n: t["ttlb_ms"].get(key) for n, t in targets.items()}, article, True))
        rows.append(("TCP connect (median)", {n: t["tcp_connect_ms"] for n, t in targets.items()}, {}, False))
        threads = f"{latency['threads']} thread{'s' if latency['threads'] > 1 else ''}"
        note = "" if comparable else ("Blog values hidden: the blog read 10,000 of 1,000,000 64 KiB objects "
                                      "with one request at a time.")
        sections.append((f"Latency (ms): {latency['reads']:,} random reads of {fmt_size(latency['object_size'])} "
                         f"from {latency['objects']:,} objects, time to last byte, {threads}", rows, note))
    throughput = summary.get("throughput")
    if throughput:
        targets = throughput["targets"]
        comparable = throughput_comparable(throughput)
        rows = []
        for label, key in (("Upload (AzCopy)", "upload"), ("Download (AzCopy)", "download"), ("List (5,000/page)", "list")):
            rows.append((label, {n: t[f"{key}_s"]["median"] for n, t in targets.items()},
                         dict(ARTICLE["seconds"][key]) if comparable else {}, True))
        note = "" if comparable else "Blog values hidden: the blog used 100,000 64 KiB objects."
        sections.append((f"Throughput (seconds): {throughput['files']:,} x {fmt_size(throughput['file_size'])} "
                         f"objects, RAM disk, median of {throughput['iterations']} "
                         f"run{'s' if throughput['iterations'] > 1 else ''}", rows, note))
    return sections


def _header_line(summary: dict) -> str:
    env = summary["environment"]
    return (f"{summary['generated_utc']} | {env.get('location')} | {env.get('vm_size')} ({env.get('cpus')} vCPU, "
            f"{env.get('memory_gib')} GiB) | AzCopy {env.get('azcopy')} | blobperf {summary['tool_version']}")


def render_markdown(summary: dict) -> str:
    lines = ["# Premium vs Standard block blob storage", "", _header_line(summary)]
    latency = summary.get("latency")
    if latency:
        paths = ", ".join(f"{n} via {t.get('network_path') or 'unknown path'} ({t.get('ip')})"
                          for n, t in latency["targets"].items())
        lines += ["", f"Network: {paths}"]
    for title, rows, note in _table_rows(summary):
        names = list(rows[0][1])
        compare = "premium" in names and "standard" in names
        lines += ["", f"## {title}", "",
                  "| | " + " | ".join(n.title() for n in names) + (" | Std/Prem" if compare else "") +
                  " | Blog Premium | Blog Standard |",
                  "|---|" + "---:|" * (len(names) + (1 if compare else 0) + 2)]
        for label, values, article, show_ratio in rows:
            cells = [fmt_num(values.get(n)) for n in names]
            if compare:
                cells.append(_ratio(values) if show_ratio else "-")
            cells += [fmt_num(article.get("premium")), fmt_num(article.get("standard"))]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        if note:
            lines += ["", note]
    lines += ["", "Std/Prem: how many times longer Standard took than Premium. Blog: published results, "
                  "D32 v3 VM, November 2018."]
    return "\n".join(lines) + "\n"


def nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    base = 10 ** math.floor(math.log10(value))
    for multiple in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= multiple * base:
            return multiple * base
    return 10 * base


def svg_bar_chart(title: str, unit: str, categories: list[str], series: list[tuple[str, str, list]],
                  ymax: float | None = None, width: int = 520, height: int = 320) -> str:
    """Grouped bar chart as inline SVG. series = [(label, colour, [value per category])]."""
    left, right, top, bottom = 58, 16, 44, 64
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [v for _, _, vals in series for v in vals if v is not None]
    ymax = ymax or nice_ceiling(max(values) if values else 1)
    e = html.escape
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{e(title)}" '
           f'font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="12">',
           f'<text x="{width / 2}" y="24" text-anchor="middle" font-size="15" fill="#333">{e(title)}</text>']
    for tick in range(6):
        y = top + plot_h - plot_h * tick / 5
        out.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e6e6e6"/>'
                   f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#666">{round(ymax * tick / 5, 3):g}</text>')
    out.append(f'<text transform="translate(16 {top + plot_h / 2:.1f}) rotate(-90)" text-anchor="middle" fill="#666">{e(unit)}</text>')
    group = plot_w / max(1, len(categories))
    bar = group * 0.7 / max(1, len(series))
    for ci, category in enumerate(categories):
        for si, (label, colour, vals) in enumerate(series):
            value = vals[ci]
            if value is None:
                continue
            h = plot_h * min(value, ymax) / ymax
            x, y = left + ci * group + group * 0.15 + si * bar, top + plot_h - h
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar * 0.9:.1f}" height="{h:.1f}" fill="{colour}">'
                       f'<title>{e(label)} - {e(category)}: {fmt_num(value)} {e(unit)}</title></rect>'
                       f'<text x="{x + bar * 0.45:.1f}" y="{y - 4:.1f}" text-anchor="middle" fill="#333">{fmt_num(value)}</text>')
        out.append(f'<text x="{left + ci * group + group / 2:.1f}" y="{top + plot_h + 18:.1f}" text-anchor="middle" '
                   f'fill="#333">{e(category)}</text>')
    x = left + plot_w / 2 - 55 * len(series)
    for label, colour, _ in series:
        out.append(f'<rect x="{x:.1f}" y="{height - 26}" width="12" height="12" fill="{colour}"/>'
                   f'<text x="{x + 17:.1f}" y="{height - 16}" fill="#333">{e(label)}</text>')
        x += 110
    out.append("</svg>")
    return "".join(out)


def render_html(summary: dict) -> str:
    e = html.escape
    charts, tables = [], []
    latency, throughput = summary.get("latency"), summary.get("throughput")
    names: list[str] = []
    for section in (latency, throughput):
        for name in (section or {}).get("targets", {}):
            if name not in names:
                names.append(name)
    colour = {n: COLORS.get(n, EXTRA_COLORS[i % len(EXTRA_COLORS)]) for i, n in enumerate(names)}
    if latency:
        cats = ["Average", "P99"]
        run = [(n.title(), colour[n], [t["ttlb_ms"].get("avg"), t["ttlb_ms"].get("p99")]) for n, t in latency["targets"].items()]
        charts.append(svg_bar_chart("Latency - this run", "milliseconds", cats, run))
        if latency_comparable(latency):
            blog = [(n.title(), COLORS[n], [ARTICLE["latency_ms"][n]["avg"], ARTICLE["latency_ms"][n]["p99"]])
                    for n in ("premium", "standard")]
            charts.append(svg_bar_chart("Latency - blog post (2018)", "milliseconds", cats, blog))
    if throughput:
        cats = ["Upload", "Download", "List"]
        run = [(n.title(), colour[n], [t["upload_s"]["median"], t["download_s"]["median"], t["list_s"]["median"]])
               for n, t in throughput["targets"].items()]
        charts.append(svg_bar_chart(f"{throughput['files']:,} objects - this run", "seconds", cats, run))
        if throughput_comparable(throughput):
            blog = [(n.title(), COLORS[n], [ARTICLE["seconds"][k][n] for k in ("upload", "download", "list")])
                    for n in ("premium", "standard")]
            charts.append(svg_bar_chart("100,000 objects - blog post (2018)", "seconds", cats, blog))
    for title, rows, note in _table_rows(summary):
        row_names = list(rows[0][1])
        compare = "premium" in row_names and "standard" in row_names
        head = "".join(f"<th>{e(n.title())}</th>" for n in row_names) + ("<th>Std/Prem</th>" if compare else "")
        body = []
        for label, values, article, show_ratio in rows:
            cells = "".join(f"<td>{fmt_num(values.get(n))}</td>" for n in row_names)
            if compare:
                cells += f"<td>{_ratio(values) if show_ratio else '-'}</td>"
            cells += f"<td class='blog'>{fmt_num(article.get('premium'))}</td><td class='blog'>{fmt_num(article.get('standard'))}</td>"
            body.append(f"<tr><th>{e(label)}</th>{cells}</tr>")
        tables.append(f"<h2>{e(title)}</h2><table><tr><th></th>{head}<th>Blog Premium</th><th>Blog Standard</th></tr>"
                      + "".join(body) + "</table>" + (f"<p class='muted'>{e(note)}</p>" if note else ""))
    env = summary["environment"]
    env_rows = "".join(f"<tr><th>{e(k.replace('_', ' '))}</th><td>{e(str(v))}</td></tr>" for k, v in env.items())
    accounts = ""
    if latency:
        accounts = "".join(f"<tr><th>{e(n.title())}</th><td>{e(t['account'])} ({e(str(t.get('ip')))}, "
                           f"{e(str(t.get('network_path')))})</td></tr>"
                           for n, t in latency["targets"].items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Premium vs Standard block blob storage</title>
<style>
body{{font-family:"Segoe UI",Helvetica,Arial,sans-serif;color:#222;max-width:1120px;margin:24px auto;padding:0 16px}}
h1{{font-weight:600}} h2{{font-size:1.05em;margin-top:28px}}
.charts{{display:flex;flex-wrap:wrap;gap:16px}} .charts svg{{width:520px;max-width:100%;border:1px solid #eee;border-radius:6px}}
table{{border-collapse:collapse;margin:6px 0 12px}} th,td{{border:1px solid #ddd;padding:5px 10px;text-align:right}}
th:first-child{{text-align:left;font-weight:500}} td.blog{{color:#777}} .muted{{color:#666;font-size:.9em}}
.env th{{text-align:left}} .env td{{text-align:left}}
</style></head><body>
<h1>Premium vs Standard block blob storage</h1>
<p class="muted">{e(_header_line(summary))}<br>Replication of the tests in
<a href="https://azure.microsoft.com/en-us/blog/premium-block-blob-storage-a-new-level-of-performance/">Premium Block Blob
Storage - a new level of performance</a> (Azure blog, November 2018). Lower is better everywhere.</p>
<div class="charts">{''.join(charts)}</div>
{''.join(tables)}
<p class="muted">Std/Prem: how many times longer Standard took than Premium. Blog values: published results from a
D32 v3 VM in November 2018.</p>
<h2>Environment</h2><table class="env">{accounts}{env_rows}</table>
</body></html>
"""


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    latency_path = getattr(args, "latency_file", None) or latest_result(cfg, "latency")
    throughput_path = getattr(args, "throughput_file", None) or latest_result(cfg, "throughput")
    latency, throughput = read_json_file(latency_path), read_json_file(throughput_path)
    if not latency and not throughput:
        raise SystemExit(f"error: no results in {cfg.results_dir}; run a test first")
    summary = build_summary(latency, throughput)
    summary["sources"] = [os.path.basename(p) for p in (latency_path, throughput_path) if p and read_json_file(p)]
    markdown, page = render_markdown(summary), render_html(summary)
    stamp = utc_stamp()
    write_json_atomic(os.path.join(cfg.results_dir, f"report-{stamp}.json"), summary)
    for suffix, content in (("md", markdown), ("html", page)):
        with open(os.path.join(cfg.results_dir, f"report-{stamp}.{suffix}"), "w", encoding="utf-8") as handle:
            handle.write(content)
    for suffix in ("json", "md", "html"):
        shutil.copyfile(os.path.join(cfg.results_dir, f"report-{stamp}.{suffix}"),
                        os.path.join(cfg.results_dir, f"latest.{suffix}"))
    printed = getattr(args, "print", "md")
    if printed == "md":
        print(markdown, flush=True)
    elif printed == "json":
        print(json.dumps(summary, indent=2), flush=True)
    return 0


# --------------------------------------------------------------------------------------------
# background jobs (used by scripts/demo.* through `az vm run-command`, or over SSH)
# --------------------------------------------------------------------------------------------

def job_path(cfg: Config) -> str:
    return os.path.join(cfg.state_dir, "job.json")


def unit_active(unit: str) -> bool:
    return bool(unit) and subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def finish_job(cfg: Config, job_id: str, exit_code: int) -> None:
    job = read_json_file(job_path(cfg))
    if job and job.get("id") == job_id:
        job.update(state="succeeded" if exit_code == 0 else "failed", exit_code=exit_code, finished_utc=utc_iso())
        write_json_atomic(job_path(cfg), job)


def cmd_start(cfg: Config, args: argparse.Namespace) -> int:
    command = [c for c in args.job_command]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or command[0] in ("start", "status", "export") or command[0].startswith("-"):
        raise SystemExit("error: usage: blobperf start <command> [options], e.g. blobperf start run-all")
    if os.geteuid() != 0:
        raise SystemExit("error: 'blobperf start' needs root (sudo)")
    job = read_json_file(job_path(cfg))
    if job and job.get("state") == "running" and unit_active(job.get("unit", "")):
        raise SystemExit(f"error: job {job['id']} is still running (see 'blobperf status')")
    os.makedirs(cfg.log_dir, exist_ok=True)
    job_id = utc_stamp()
    unit = f"blobperf-{job_id}"
    log_file = os.path.join(cfg.log_dir, f"job-{job_id}.log")
    write_json_atomic(job_path(cfg), {"id": job_id, "unit": unit, "command": command, "log": log_file,
                                      "state": "running", "started_utc": utc_iso()})
    passthrough = [f"--setenv={k}={v}" for k, v in os.environ.items() if k.startswith(("BLOBPERF_", "AZCOPY_"))]
    subprocess.run(["systemd-run", f"--unit={unit}", "--collect", "--quiet",
                    f"--property=StandardOutput=append:{log_file}", f"--property=StandardError=append:{log_file}",
                    "--setenv=PYTHONUNBUFFERED=1", *passthrough,
                    sys.executable, os.path.abspath(__file__), "--job-id", job_id, *command], check=True)
    print(f"started job {job_id}: blobperf {' '.join(command)}\nlog: {log_file}")
    return 0


def tail(path: str, lines: int) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 256 * KIB))
            return handle.read().decode("utf-8", "replace").splitlines()[-lines:] if lines > 0 else []
    except OSError:
        return []


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    job = read_json_file(job_path(cfg))
    if not job:
        print("JOB - STATE: none")
        return 0
    state, note = job.get("state", "unknown"), ""
    if state == "running" and not unit_active(job.get("unit", "")):
        state, note = "failed", " (process ended unexpectedly)"
    started = dt.datetime.strptime(job["started_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    finished = job.get("finished_utc")
    end = (dt.datetime.strptime(finished, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
           if finished else dt.datetime.now(dt.timezone.utc))
    print(f"JOB {job['id']} STATE: {state}{note} ELAPSED: {fmt_duration((end - started).total_seconds())} "
          f"CMD: blobperf {' '.join(job.get('command', []))}")
    for line in tail(job.get("log", ""), args.lines):
        print(line[:args.width])
    return 0


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    """Print the latest report bundle (tar.gz, base64) in chunks small enough for run-command output."""
    bundle = os.path.join(cfg.state_dir, "export", "bundle.tgz")
    if args.chunk == 0 or not os.path.exists(bundle):
        summary = read_json_file(os.path.join(cfg.results_dir, "latest.json"))
        if not summary:
            raise SystemExit("error: no report yet; run 'blobperf report' first")
        files = [os.path.join(cfg.results_dir, f"latest.{s}") for s in ("md", "json", "html")]
        files += [os.path.join(cfg.results_dir, name) for name in summary.get("sources", [])]
        names = {"latest.md": "summary.md", "latest.json": "summary.json", "latest.html": "report.html"}
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
    print(f"BLOBPERF-EXPORT chunk={args.chunk} total={total} sha256={hashlib.sha256(raw).hexdigest()}")
    print(data[args.chunk * args.chunk_size:(args.chunk + 1) * args.chunk_size])
    return 0


# --------------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------------

def positive_int(text: str) -> int:
    value = int(str(text).replace(",", "").replace("_", ""))
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def non_negative_int(text: str) -> int:
    value = int(str(text).replace(",", "").replace("_", ""))
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def thread_count(text: str) -> int:
    value = positive_int(text)
    if value > 256:
        raise argparse.ArgumentTypeError("must be between 1 and 256")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blobperf", description=__doc__.split("\n\n")[0], epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"blobperf {VERSION}")
    parser.add_argument("--job-id", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(metavar="<command>")

    def add(name: str, func, text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=text, description=text)
        sub.set_defaults(func=func)
        return sub

    def targets(sub):
        sub.add_argument("--targets", default="premium,standard",
                         help="comma separated accounts from the config, in test order (default: %(default)s)")

    def size(sub):
        sub.add_argument("--size", type=parse_size, default=64 * KIB, help="object size, e.g. 65536 or 64KiB (default: 64KiB)")

    def dataset(sub):
        sub.add_argument("--container", default="latency", help="container holding the latency dataset (default: %(default)s)")

    def load(sub):
        sub.add_argument("--objects", type=positive_int, default=1_000_000, help="dataset size (default: 1,000,000)")
        sub.add_argument("--batch", type=positive_int, default=100_000, help="objects generated + uploaded per batch (default: 100,000)")
        sub.add_argument("--force", action="store_true", help="regenerate and upload even if the dataset exists")

    def latency(sub):
        sub.add_argument("--reads", type=positive_int, default=10_000, help="measured random reads (default: 10,000)")
        sub.add_argument("--warmup", type=non_negative_int, default=50, help="unmeasured reads to open the connection (default: 50)")
        sub.add_argument("--threads", type=thread_count, default=1, help="concurrent readers (1-256), 1 = one request at a time (default: 1)")
        sub.add_argument("--seed", type=int, help="random seed for the object selection (default: random)")

    def throughput(sub):
        sub.add_argument("--files", type=positive_int, default=100_000, help="objects to upload/download/list (default: 100,000)")
        sub.add_argument("--iterations", type=positive_int, default=3, help="repetitions per account; the median is reported (default: 3)")
        sub.add_argument("--keep", action="store_true", help="keep the uploaded containers (tp-*)")

    def azcopy(sub):
        sub.add_argument("--azcopy-concurrency", type=positive_int,
                         help="AZCOPY_CONCURRENCY_VALUE for the AzCopy jobs (default: AzCopy's own default)")

    sub = add("info", cmd_info, "show configuration, VM details and access to the storage accounts")
    targets(sub)
    sub = add("load", cmd_load, "load the latency dataset (1,000,000 x 64 KiB random objects) into each account")
    for option in (targets, size, dataset, load, azcopy):
        option(sub)
    sub = add("latency", cmd_latency, "latency test: random reads from the dataset, time to last byte")
    for option in (targets, dataset, latency):
        option(sub)
    sub = add("throughput", cmd_throughput, "AzCopy upload + download of 100,000 x 64 KiB files, then list them")
    for option in (targets, size, throughput, azcopy):
        option(sub)
    sub = add("list", cmd_list, "time listing every blob in a container")
    targets(sub)
    sub.add_argument("container")
    sub.add_argument("--prefix")
    sub = add("run-all", cmd_run_all, "load the dataset if needed, then run the latency and throughput tests and report")
    for option in (targets, size, dataset, load, latency, throughput, azcopy):
        option(sub)
    sub.set_defaults(print="md", latency_file=None, throughput_file=None)
    sub = add("report", cmd_report, "summarize the latest results (markdown, JSON and HTML in the results folder)")
    sub.add_argument("--latency-file", help="latency result to use (default: latest)")
    sub.add_argument("--throughput-file", help="throughput result to use (default: latest)")
    sub.add_argument("--print", choices=("md", "json", "none"), default="md", help="what to print (default: md)")
    sub = add("start", cmd_start, "run a command as a background job, e.g. 'blobperf start run-all'")
    sub.add_argument("job_command", nargs=argparse.REMAINDER, metavar="<command> [options]")
    sub = add("status", cmd_status, "state of the background job and the end of its log")
    sub.add_argument("--lines", type=int, default=15, help="log lines to show (default: 15)")
    sub.add_argument("--width", type=int, default=200, help="truncate log lines to this width (default: 200)")
    sub = add("export", cmd_export, "print the latest report bundle as base64 chunks (used by scripts/demo.*)")
    sub.add_argument("--chunk", type=int, default=0)
    sub.add_argument("--chunk-size", type=positive_int, default=2800)
    sub = add("cleanup", cmd_cleanup, "delete leftover throughput containers (tp-*)")
    targets(sub)
    sub.add_argument("--dataset", action="store_true", help="also delete the latency dataset container")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    cfg = Config()
    try:
        exit_code = int(args.func(cfg, args) or 0)
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
        finish_job(cfg, args.job_id, exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
