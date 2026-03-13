from __future__ import annotations

import os
import re
import socket
import statistics
import subprocess
import threading
import time
import urllib.request
import ssl
from typing import Any


AIRPORT_PATH = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
DEFAULT_PRIMARY_TARGET = "1.1.1.1"
DEFAULT_SECONDARY_TARGET = "8.8.8.8"
DNS_HOSTS = ["zoom.us", "meet.google.com", "teams.microsoft.com", "webex.com"]
SPEED_TEST_USER_AGENT = "Wi-Fi Readiness/1.0"


def run_command(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        return output.strip()
    except (subprocess.SubprocessError, FileNotFoundError, TimeoutError):
        return ""


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (q / 100.0)
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    fraction = position - left
    return ordered[left] + (ordered[right] - ordered[left]) * fraction


def compute_jitter_ms(latencies_ms: list[float]) -> float:
    if len(latencies_ms) < 2:
        return 0.0
    deltas = [abs(latencies_ms[i] - latencies_ms[i - 1]) for i in range(1, len(latencies_ms))]
    return float(statistics.mean(deltas))


def detect_spikes(latencies_ms: list[float], baseline_ms: float | None = None) -> int:
    if not latencies_ms:
        return 0
    effective_baseline = baseline_ms if baseline_ms is not None else percentile(latencies_ms, 50) or 0.0
    threshold = max(25.0, effective_baseline * 2.0)
    return sum(1 for value in latencies_ms if value > threshold)


def summarize_latency(latencies_ms: list[float], sent: int) -> dict[str, Any]:
    p50 = percentile(latencies_ms, 50)
    p95 = percentile(latencies_ms, 95)
    jitter = compute_jitter_ms(latencies_ms)
    received = len(latencies_ms)
    packet_loss = ((sent - received) / sent * 100.0) if sent > 0 else 100.0

    return {
        "sent": sent,
        "received": received,
        "packet_loss_pct": round(packet_loss, 2),
        "latency_p50_ms": round(p50, 2) if p50 is not None else None,
        "latency_p95_ms": round(p95, 2) if p95 is not None else None,
        "jitter_ms": round(jitter, 2),
        "spike_count": detect_spikes(latencies_ms, p50),
        "dropout_count": max(0, sent - received),
    }


def parse_ping_output(output: str, sent_hint: int | None = None) -> dict[str, Any]:
    latencies = [float(value) for value in re.findall(r"time[=<]([\d.]+)\s*ms", output)]
    sent = sent_hint if sent_hint is not None else len(latencies)
    received = len(latencies)
    packet_loss_pct = 0.0 if sent > 0 else 100.0

    stats_match = re.search(
        r"(\d+)\s+packets transmitted,\s+(\d+)\s+packets received,\s+([\d.]+)% packet loss",
        output,
    )
    if stats_match:
        sent = int(stats_match.group(1))
        received = int(stats_match.group(2))
        packet_loss_pct = float(stats_match.group(3))
    elif sent > 0:
        packet_loss_pct = ((sent - received) / sent) * 100.0

    return {
        "sent": sent,
        "received": received,
        "packet_loss_pct": round(packet_loss_pct, 2),
        "latencies_ms": latencies,
    }


def parse_airport_output(output: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "rssi_dbm": None,
        "noise_dbm": None,
        "snr_db": None,
        "tx_rate_mbps": None,
        "channel": None,
        "band": None,
    }
    patterns = {
        "rssi_dbm": r"agrCtlRSSI:\s*(-?\d+)",
        "noise_dbm": r"agrCtlNoise:\s*(-?\d+)",
        "tx_rate_mbps": r"lastTxRate:\s*(\d+)",
        "channel": r"channel:\s*([^\n]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if not match:
            continue
        raw = match.group(1).strip()
        metrics[key] = int(raw) if key != "channel" else raw

    if metrics["rssi_dbm"] is not None and metrics["noise_dbm"] is not None:
        metrics["snr_db"] = metrics["rssi_dbm"] - metrics["noise_dbm"]

    channel_value = metrics["channel"]
    if channel_value:
        primary_channel = str(channel_value).split(",")[0].strip()
        if primary_channel.isdigit():
            metrics["band"] = "2.4GHz" if int(primary_channel) <= 14 else "5GHz/6GHz"
    return metrics


def parse_system_profiler_output(output: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "rssi_dbm": None,
        "noise_dbm": None,
        "snr_db": None,
        "tx_rate_mbps": None,
        "channel": None,
        "band": None,
    }
    signal_noise = re.search(r"Signal\s*/\s*Noise:\s*(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm", output)
    if signal_noise:
        metrics["rssi_dbm"] = int(signal_noise.group(1))
        metrics["noise_dbm"] = int(signal_noise.group(2))
        metrics["snr_db"] = metrics["rssi_dbm"] - metrics["noise_dbm"]

    tx_rate = re.search(r"Transmit Rate:\s*(\d+)", output)
    if tx_rate:
        metrics["tx_rate_mbps"] = int(tx_rate.group(1))

    channel = re.search(r"Channel:\s*(\d+)", output)
    if channel:
        metrics["channel"] = channel.group(1)
        metrics["band"] = "2.4GHz" if int(metrics["channel"]) <= 14 else "5GHz/6GHz"
    return metrics


def _wifi_interface_from_networksetup() -> str | None:
    output = run_command(["networksetup", "-listallhardwareports"], timeout=5.0)
    if not output:
        return None
    port_match = re.search(r"Hardware Port:\s*(Wi-Fi|AirPort)\nDevice:\s*([a-z0-9]+)", output, re.IGNORECASE)
    if port_match:
        return port_match.group(2)
    return None


def get_default_interface() -> str | None:
    route_output = run_command(["route", "get", "default"], timeout=5.0)
    match = re.search(r"interface:\s+([a-z0-9]+)", route_output)
    if match:
        return match.group(1)
    return _wifi_interface_from_networksetup()


def get_gateway_ip() -> str | None:
    route_output = run_command(["route", "get", "default"], timeout=5.0)
    match = re.search(r"gateway:\s+([0-9.]+)", route_output)
    return match.group(1) if match else None


def get_wifi_info() -> dict[str, Any]:
    interface = _wifi_interface_from_networksetup() or get_default_interface()
    info = {
        "interface": interface,
        "ssid": None,
        "rssi_dbm": None,
        "noise_dbm": None,
        "snr_db": None,
        "tx_rate_mbps": None,
        "channel": None,
        "band": None,
    }
    if not interface:
        return info

    ssid_output = run_command(["networksetup", "-getairportnetwork", interface], timeout=5.0)
    ssid_match = re.search(r"Current (?:Wi-Fi|AirPort) Network:\s*(.+)", ssid_output)
    if ssid_match:
        info["ssid"] = ssid_match.group(1).strip()

    if os.path.exists(AIRPORT_PATH):
        airport_output = run_command([AIRPORT_PATH, "-I"], timeout=5.0)
        parsed = parse_airport_output(airport_output)
        info.update(parsed)
    if info["rssi_dbm"] is None or info["tx_rate_mbps"] is None:
        profiler_output = run_command(["system_profiler", "SPAirPortDataType"], timeout=15.0)
        parsed = parse_system_profiler_output(profiler_output)
        for key, value in parsed.items():
            if info.get(key) is None and value is not None:
                info[key] = value
    return info


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def ping_target(target: str, count: int, interval: float) -> dict[str, Any]:
    timeout = max(10.0, count * interval + 8.0)
    output = run_command(
        ["ping", "-c", str(count), "-i", f"{interval:.2f}", target],
        timeout=timeout,
    )
    parsed = parse_ping_output(output, sent_hint=count)
    parsed["target"] = target
    return parsed


def dns_latency(hosts: list[str] | None = None, attempts: int = 2) -> dict[str, Any]:
    hosts = hosts or DNS_HOSTS
    samples: list[float] = []
    failures = 0
    for host in hosts:
        for _ in range(attempts):
            start = time.perf_counter()
            try:
                socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                samples.append(elapsed_ms)
            except socket.gaierror:
                failures += 1
    return {
        "dns_samples": len(samples),
        "dns_failures": failures,
        "dns_p50_ms": round(percentile(samples, 50) or 0.0, 2) if samples else None,
        "dns_p95_ms": round(percentile(samples, 95) or 0.0, 2) if samples else None,
    }


def route_snapshot(target: str = DEFAULT_PRIMARY_TARGET) -> dict[str, Any]:
    output = run_command(["traceroute", "-m", "8", "-q", "1", "-w", "1", target], timeout=20.0)
    hop_lines = [line for line in output.splitlines() if re.match(r"^\s*\d+\s", line)]
    timeout_hops = sum(1 for line in hop_lines if "*" in line)
    return {
        "hop_count": len(hop_lines),
        "route_timeout_hops": timeout_hops,
    }


def _timed_download(url: str, read_bytes: int, context: ssl.SSLContext) -> float | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": SPEED_TEST_USER_AGENT})
        start = time.perf_counter()
        with urllib.request.urlopen(request, timeout=20.0, context=context) as response:
            payload = response.read(read_bytes)
        elapsed = max(time.perf_counter() - start, 0.001)
        return round((len(payload) * 8.0) / (elapsed * 1_000_000.0), 2)
    except Exception:
        return None


def _timed_upload(url: str, payload: bytes, context: ssl.SSLContext) -> float | None:
    try:
        request = urllib.request.Request(
            url,
            method="POST",
            data=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "User-Agent": SPEED_TEST_USER_AGENT,
            },
        )
        start = time.perf_counter()
        with urllib.request.urlopen(request, timeout=20.0, context=context) as response:
            response.read(16)
        elapsed = max(time.perf_counter() - start, 0.001)
        return round((len(payload) * 8.0) / (elapsed * 1_000_000.0), 2)
    except Exception:
        return None


def congestion_probe(
    target: str,
    baseline_p95_ms: float | None,
    download_bytes: int = 600_000,
    upload_bytes: int = 300_000,
) -> dict[str, Any]:
    context = _build_ssl_context()
    load_results: dict[str, float | None] = {"download_mbps": None, "upload_mbps": None}

    def load_task() -> None:
        load_results["download_mbps"] = _timed_download(
            f"https://speed.cloudflare.com/__down?bytes={download_bytes}",
            read_bytes=download_bytes,
            context=context,
        )
        load_results["upload_mbps"] = _timed_upload(
            "https://speed.cloudflare.com/__up",
            payload=os.urandom(upload_bytes),
            context=context,
        )

    # Run upload/download pressure while measuring latency to expose bufferbloat risk.
    worker = threading.Thread(target=load_task, daemon=True)
    worker.start()

    post_ping = ping_target(target, count=10, interval=0.3)
    worker.join(timeout=25.0)
    post_summary = summarize_latency(post_ping["latencies_ms"], post_ping["sent"])
    post_p95 = post_summary["latency_p95_ms"]
    if baseline_p95_ms is None or post_p95 is None:
        delta = None
    else:
        delta = round(post_p95 - baseline_p95_ms, 2)
    return {
        "post_load_p95_ms": post_p95,
        "congestion_delta_ms": delta,
        "download_mbps": load_results["download_mbps"],
        "upload_mbps": load_results["upload_mbps"],
    }


def optional_speed_test(download_bytes: int = 2_000_000, upload_bytes: int = 1_000_000) -> dict[str, Any]:
    results: dict[str, Any] = {
        "download_mbps": None,
        "upload_mbps": None,
        "error": None,
    }
    try:
        context = _build_ssl_context()
        down_url = f"https://speed.cloudflare.com/__down?bytes={download_bytes}"
        down_request = urllib.request.Request(
            down_url,
            headers={"User-Agent": SPEED_TEST_USER_AGENT},
        )
        start = time.perf_counter()
        with urllib.request.urlopen(down_request, timeout=20.0, context=context) as response:
            payload = response.read()
        elapsed = max(time.perf_counter() - start, 0.001)
        results["download_mbps"] = round((len(payload) * 8.0) / (elapsed * 1_000_000.0), 2)

        up_payload = os.urandom(upload_bytes)
        request = urllib.request.Request(
            "https://speed.cloudflare.com/__up",
            method="POST",
            data=up_payload,
            headers={
                "Content-Type": "application/octet-stream",
                "User-Agent": SPEED_TEST_USER_AGENT,
            },
        )
        start = time.perf_counter()
        with urllib.request.urlopen(request, timeout=20.0, context=context) as response:
            response.read(16)
        elapsed = max(time.perf_counter() - start, 0.001)
        results["upload_mbps"] = round((upload_bytes * 8.0) / (elapsed * 1_000_000.0), 2)
    except Exception as exc:
        results["error"] = str(exc)
    return results


def gather_assessment(duration_minutes: int = 2, include_speed_test: bool = False) -> dict[str, Any]:
    duration_seconds = max(60, int(duration_minutes * 60))
    sustained_count = max(12, int(duration_seconds / 5))

    wifi = get_wifi_info()
    gateway_ip = get_gateway_ip()

    sustained = ping_target(DEFAULT_PRIMARY_TARGET, count=sustained_count, interval=5.0)
    short_count = max(6, min(24, int(sustained_count / 4)))
    secondary = ping_target(DEFAULT_SECONDARY_TARGET, count=short_count, interval=1.0)
    gateway = ping_target(gateway_ip, count=short_count, interval=1.0) if gateway_ip else None

    merged_latencies = sustained["latencies_ms"] + secondary["latencies_ms"]
    merged_sent = sustained["sent"] + secondary["sent"]
    merged_summary = summarize_latency(merged_latencies, sent=merged_sent)

    dns = dns_latency()
    route = route_snapshot(DEFAULT_PRIMARY_TARGET)
    congestion = congestion_probe(DEFAULT_PRIMARY_TARGET, merged_summary["latency_p95_ms"])

    metrics = {
        "latency_p50_ms": merged_summary["latency_p50_ms"],
        "latency_p95_ms": merged_summary["latency_p95_ms"],
        "jitter_ms": merged_summary["jitter_ms"],
        "packet_loss_pct": merged_summary["packet_loss_pct"],
        "spike_count": merged_summary["spike_count"],
        "dropout_count": merged_summary["dropout_count"],
        "snr_db": wifi.get("snr_db"),
        "rssi_dbm": wifi.get("rssi_dbm"),
        "noise_dbm": wifi.get("noise_dbm"),
        "tx_rate_mbps": wifi.get("tx_rate_mbps"),
        "dns_p95_ms": dns["dns_p95_ms"],
        "dns_failures": dns["dns_failures"],
        "route_timeout_hops": route["route_timeout_hops"],
        "congestion_delta_ms": congestion["congestion_delta_ms"],
    }

    result: dict[str, Any] = {
        "metrics": metrics,
        "raw": {
            "wifi": wifi,
            "gateway_ip": gateway_ip,
            "sustained_ping": sustained,
            "secondary_ping": secondary,
            "gateway_ping": gateway,
            "dns": dns,
            "route": route,
            "congestion": congestion,
        },
    }
    if include_speed_test:
        result["speed_test"] = optional_speed_test()
    return result
