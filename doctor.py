"""WiFi Doctor — automated diagnosis and one-click fixes.

Runs script-based checks, identifies root causes, and offers safe
automated fixes that the user can approve one by one.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from typing import Any, Callable

from probes import run_command, percentile, DNS_HOSTS


# ── Human-friendly metric names & impact descriptions ───────
# Used across UI, reports, and doctor recommendations.

FRIENDLY_NAMES: dict[str, str] = {
    "latency_p95_ms": "Call Delay",
    "jitter_ms": "Voice Stability",
    "packet_loss_pct": "Dropped Audio/Video",
    "snr_db": "Wi-Fi Signal Quality",
    "dns_p95_ms": "App & Page Load Speed",
    "congestion_delta_ms": "Slowdown Under Load",
    "route_timeout_hops": "Network Path Health",
    "download_mbps": "Download Speed",
    "upload_mbps": "Upload Speed",
    "secondary_packet_loss_pct": "Backup Route Reliability",
    "secondary_latency_p95_ms": "Backup Route Delay",
    "gateway_packet_loss_pct": "Router Connection Drops",
    "gateway_latency_p95_ms": "Router Response Time",
    "rssi_dbm": "Signal Strength",
    "noise_dbm": "Background Interference",
    "tx_rate_mbps": "Wi-Fi Link Speed",
    "spike_count": "Lag Spikes",
    "dropout_count": "Total Dropouts",
    "dns_failures": "Failed Website Lookups",
    "latency_p50_ms": "Typical Delay",
}

# What the user actually experiences when a metric is bad.
IMPACT_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "latency_p95_ms": {
        "bad": "Others hear you with a noticeable delay — like a satellite call.",
        "ok": "Slight delay but most people won't notice.",
        "good": "Conversations feel instant and natural.",
    },
    "jitter_ms": {
        "bad": "Voice sounds robotic or choppy. Video stutters.",
        "ok": "Occasional minor glitches, mostly fine.",
        "good": "Smooth, clear audio and video throughout.",
    },
    "packet_loss_pct": {
        "bad": "Video freezes and audio cuts out mid-sentence.",
        "ok": "Rare brief audio blips. Usually fine.",
        "good": "Crystal clear — nothing gets lost.",
    },
    "snr_db": {
        "bad": "Weak Wi-Fi signal — like talking through a wall. Move closer to router.",
        "ok": "Adequate signal, but walls or distance may cause issues.",
        "good": "Strong, clean signal — ideal for video calls.",
    },
    "dns_p95_ms": {
        "bad": "Apps and websites take a long time to connect initially.",
        "ok": "Slight delay when opening new pages or joining calls.",
        "good": "Everything loads instantly.",
    },
    "congestion_delta_ms": {
        "bad": "Everything slows down when someone else on the network is downloading.",
        "ok": "Some slowdown during heavy usage but manageable.",
        "good": "Your call stays smooth even when others are streaming.",
    },
    "route_timeout_hops": {
        "bad": "Your internet path has dead spots — data takes detours.",
        "ok": "Minor routing hiccup, usually harmless.",
        "good": "Clear, direct path to the internet.",
    },
    "download_mbps": {
        "bad": "Not enough bandwidth for video — expect pixelated or frozen video.",
        "ok": "Enough for basic video but may struggle with screen sharing.",
        "good": "Plenty of bandwidth for HD video and screen sharing.",
    },
    "upload_mbps": {
        "bad": "Others will see you as frozen or very pixelated.",
        "ok": "Your video quality may drop under heavy use.",
        "good": "Others see you clearly in HD.",
    },
    "gateway_packet_loss_pct": {
        "bad": "Data is getting lost between your device and your router.",
        "ok": "Occasional drops at the router level.",
        "good": "Solid connection to your router.",
    },
    "gateway_latency_p95_ms": {
        "bad": "Your router is responding slowly — it may be overloaded.",
        "ok": "Router is a bit slow but usually fine.",
        "good": "Router responds instantly.",
    },
}

# Radar-friendly short labels (for the signal radar UI).
RADAR_LABELS: dict[str, tuple[str, str]] = {
    "latency_p95_ms": ("⏱️  Call Delay", "ms"),
    "jitter_ms": ("📈 Voice Stability", "ms"),
    "packet_loss_pct": ("📦 Dropped Audio", "%"),
    "snr_db": ("📶 Signal Quality", "dB"),
    "dns_p95_ms": ("🌐 Load Speed", "ms"),
    "congestion_delta_ms": ("🔥 Under-Load Lag", "ms"),
    "download_mbps": ("⬇️  Download", "Mbps"),
    "upload_mbps": ("⬆️  Upload", "Mbps"),
}

# Short one-liner for each radar metric when it's bad.
RADAR_ONE_LINERS: dict[str, dict[str, str]] = {
    "latency_p95_ms": {
        "PASS": "Calls feel instant",
        "WARN": "Slight delay on calls",
        "FAIL": "Noticeable lag on calls",
    },
    "jitter_ms": {
        "PASS": "Smooth audio & video",
        "WARN": "Occasional choppy audio",
        "FAIL": "Robotic/choppy voice",
    },
    "packet_loss_pct": {
        "PASS": "Nothing gets lost",
        "WARN": "Rare audio blips",
        "FAIL": "Video freezes, audio drops",
    },
    "snr_db": {
        "PASS": "Strong Wi-Fi signal",
        "WARN": "Signal could be better",
        "FAIL": "Weak signal — move closer",
    },
    "dns_p95_ms": {
        "PASS": "Fast page loads",
        "WARN": "Slow to connect",
        "FAIL": "Very slow to load apps",
    },
    "congestion_delta_ms": {
        "PASS": "Steady under load",
        "WARN": "Slows when busy",
        "FAIL": "Crawls when others download",
    },
    "download_mbps": {
        "PASS": "Fast downloads",
        "WARN": "Downloads could be faster",
        "FAIL": "Very slow downloads",
    },
    "upload_mbps": {
        "PASS": "Fast uploads",
        "WARN": "Uploads could be faster",
        "FAIL": "Others see you frozen",
    },
}


def get_friendly_name(metric: str) -> str:
    """Return a human-friendly name for a technical metric key."""
    return FRIENDLY_NAMES.get(metric, metric.replace("_", " ").title())


def get_impact_description(metric: str, severity: str) -> str:
    """Return what the user will actually experience for a given metric+severity."""
    desc = IMPACT_DESCRIPTIONS.get(metric, {})
    if severity == "FAIL":
        return desc.get("bad", "This metric needs attention.")
    if severity == "WARN":
        return desc.get("ok", "This metric is borderline.")
    return desc.get("good", "This metric looks great.")


def get_radar_label(metric: str) -> tuple[str, str]:
    """Return (emoji_label, unit) for radar display."""
    return RADAR_LABELS.get(metric, (get_friendly_name(metric), ""))


def get_radar_one_liner(metric: str, severity: str) -> str:
    """Return a short one-liner for what the metric means at given severity."""
    liners = RADAR_ONE_LINERS.get(metric, {})
    return liners.get(severity, "")


def get_friendly_recommendation(metric: str, severity: str) -> str:
    """Return a human-friendly, non-technical recommendation."""
    recs: dict[str, dict[str, str]] = {
        "latency_p95_ms": {
            "WARN": "Try moving closer to your Wi-Fi router, or connect with an ethernet cable.",
            "FAIL": "Your connection has too much delay for video calls. Use ethernet or restart your router.",
        },
        "jitter_ms": {
            "WARN": "Your connection is a bit unsteady. Close other apps using the internet.",
            "FAIL": "Your connection is very choppy. Pause any downloads and close streaming apps.",
        },
        "packet_loss_pct": {
            "WARN": "Some data is getting lost. Try restarting your router.",
            "FAIL": "Too much data is being lost for clear calls. Restart router and close other devices.",
        },
        "snr_db": {
            "WARN": "Your Wi-Fi signal is weak. Move closer to the router or remove obstacles.",
            "FAIL": "Very weak signal. Move to the same room as the router, or use ethernet.",
        },
        "dns_p95_ms": {
            "WARN": "Websites are slow to load. Our WiFi Doctor can fix this automatically!",
            "FAIL": "Very slow DNS lookups causing delays. Let WiFi Doctor fix your DNS settings!",
        },
        "congestion_delta_ms": {
            "WARN": "Your network slows down when busy. Pause other downloads during calls.",
            "FAIL": "Severe slowdown under load. Enable QoS on your router if possible.",
        },
        "route_timeout_hops": {
            "WARN": "Some network hops are slow. This usually fixes itself.",
            "FAIL": "Your internet path has issues. Try again later or contact your ISP.",
        },
        "download_mbps": {
            "WARN": "Download speed is low. Close other downloads and streaming.",
            "FAIL": "Download speed is too low for video. Check your internet plan.",
        },
        "upload_mbps": {
            "WARN": "Upload speed is low. Others may see lower quality video of you.",
            "FAIL": "Upload speed is too low. Close uploads and consider upgrading your plan.",
        },
    }
    return recs.get(metric, {}).get(severity, "")


# ── Diagnostic checks ──────────────────────────────────────

class DiagnosticResult:
    """One diagnostic check result."""

    def __init__(
        self,
        check_id: str,
        title: str,
        status: str,  # "ok", "warn", "fail"
        detail: str,
        fixable: bool = False,
        fix_description: str = "",
        fix_fn: Callable[[], tuple[bool, str]] | None = None,
    ):
        self.check_id = check_id
        self.title = title
        self.status = status
        self.detail = detail
        self.fixable = fixable
        self.fix_description = fix_description
        self.fix_fn = fix_fn

    def __repr__(self) -> str:
        icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}.get(self.status, "❓")
        return f"{icon} {self.title}: {self.detail}"


def _check_dns_servers() -> DiagnosticResult:
    """Check if fast public DNS resolvers are configured."""
    output = run_command(["networksetup", "-getdnsservers", "Wi-Fi"], timeout=5.0)
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]

    fast_dns = {"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"}
    current = set(lines)
    has_fast = bool(current & fast_dns)

    if "There aren't any DNS Servers" in output or not lines:
        return DiagnosticResult(
            check_id="dns_servers",
            title="DNS Servers",
            status="fail",
            detail="No custom DNS configured — using slow ISP defaults.",
            fixable=True,
            fix_description="Set DNS to Cloudflare (1.1.1.1) + Google (8.8.8.8) for faster lookups",
            fix_fn=_fix_dns_servers,
        )

    if not has_fast:
        return DiagnosticResult(
            check_id="dns_servers",
            title="DNS Servers",
            status="warn",
            detail=f"Custom DNS set ({', '.join(lines)}) but not using fastest resolvers.",
            fixable=True,
            fix_description="Switch to Cloudflare (1.1.1.1) + Google (8.8.8.8)",
            fix_fn=_fix_dns_servers,
        )

    if len(current & fast_dns) < 3:
        return DiagnosticResult(
            check_id="dns_servers",
            title="DNS Servers",
            status="warn",
            detail=f"Using {', '.join(current & fast_dns)} — adding more fallbacks improves reliability.",
            fixable=True,
            fix_description="Add 1.0.0.1 and 8.8.4.4 as additional fallback DNS servers",
            fix_fn=_fix_dns_servers,
        )

    return DiagnosticResult(
        check_id="dns_servers",
        title="DNS Servers",
        status="ok",
        detail=f"Fast DNS configured: {', '.join(lines)}",
    )


def _fix_dns_servers() -> tuple[bool, str]:
    """Set DNS to Cloudflare + Google with fallbacks."""
    result = run_command(
        ["networksetup", "-setdnsservers", "Wi-Fi", "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4"],
        timeout=10.0,
    )
    if "error" in result.lower() or "requires" in result.lower():
        return False, f"Could not set DNS servers: {result}"
    # Verify
    verify = run_command(["networksetup", "-getdnsservers", "Wi-Fi"], timeout=5.0)
    if "1.1.1.1" in verify:
        return True, "DNS set to 1.1.1.1, 1.0.0.1, 8.8.8.8, 8.8.4.4 ✓"
    return False, f"DNS change may not have applied: {verify}"


def _check_dns_cache() -> DiagnosticResult:
    """Check if DNS cache might be stale and causing issues."""
    # Quick test: measure system DNS for a known host
    samples: list[float] = []
    for host in ["zoom.us", "meet.google.com"]:
        start = time.perf_counter()
        try:
            socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            elapsed = (time.perf_counter() - start) * 1000.0
            samples.append(elapsed)
        except socket.gaierror:
            samples.append(999.0)

    p95 = percentile(samples, 95) if samples else None
    if p95 and p95 > 200:
        return DiagnosticResult(
            check_id="dns_cache",
            title="DNS Cache",
            status="fail",
            detail=f"DNS lookups are very slow ({p95:.0f}ms). Cache may be stale.",
            fixable=True,
            fix_description="Flush DNS cache to clear stale entries",
            fix_fn=_fix_dns_cache,
        )
    if p95 and p95 > 80:
        return DiagnosticResult(
            check_id="dns_cache",
            title="DNS Cache",
            status="warn",
            detail=f"DNS lookups are moderate ({p95:.0f}ms). Flushing cache might help.",
            fixable=True,
            fix_description="Flush DNS cache to clear stale entries",
            fix_fn=_fix_dns_cache,
        )
    return DiagnosticResult(
        check_id="dns_cache",
        title="DNS Cache",
        status="ok",
        detail=f"DNS lookups are fast ({p95:.0f}ms)." if p95 else "DNS lookups are responsive.",
    )


def _fix_dns_cache() -> tuple[bool, str]:
    """Flush macOS DNS cache."""
    # dscacheutil doesn't require sudo, killall mDNSResponder does
    run_command(["dscacheutil", "-flushcache"], timeout=5.0)
    # Try killall without sudo (may fail silently)
    run_command(["killall", "-HUP", "mDNSResponder"], timeout=5.0)
    # Verify by re-testing
    start = time.perf_counter()
    try:
        socket.getaddrinfo("zoom.us", None, proto=socket.IPPROTO_TCP)
        elapsed = (time.perf_counter() - start) * 1000.0
        return True, f"DNS cache flushed. Test lookup: {elapsed:.0f}ms ✓"
    except Exception as e:
        return True, f"Cache flushed but verification lookup failed: {e}"


def _check_search_domains() -> DiagnosticResult:
    """Check for unnecessary search domains that slow DNS."""
    output = run_command(["networksetup", "-getsearchdomains", "Wi-Fi"], timeout=5.0)
    if "There aren't any" in output or not output.strip() or output.strip().lower() == "empty":
        return DiagnosticResult(
            check_id="search_domains",
            title="Search Domains",
            status="ok",
            detail="No search domains configured (good — avoids extra lookups).",
        )
    domains = [l.strip() for l in output.strip().splitlines() if l.strip()]
    if domains and domains != ["Empty"]:
        return DiagnosticResult(
            check_id="search_domains",
            title="Search Domains",
            status="warn",
            detail=f"Search domains ({', '.join(domains)}) cause extra DNS lookups.",
            fixable=True,
            fix_description="Clear search domains to speed up DNS resolution",
            fix_fn=_fix_search_domains,
        )
    return DiagnosticResult(
        check_id="search_domains",
        title="Search Domains",
        status="ok",
        detail="Search domains are cleared.",
    )


def _fix_search_domains() -> tuple[bool, str]:
    """Clear search domains."""
    run_command(["networksetup", "-setSearchDomains", "Wi-Fi", "Empty"], timeout=5.0)
    return True, "Search domains cleared ✓"


def _check_wifi_band() -> DiagnosticResult:
    """Check if connected to 5GHz (preferred) vs 2.4GHz."""
    from probes import get_wifi_info
    wifi = get_wifi_info()
    band = wifi.get("band")
    channel = wifi.get("channel")
    ssid = wifi.get("ssid") or "Unknown"

    if not band:
        return DiagnosticResult(
            check_id="wifi_band",
            title="Wi-Fi Band",
            status="warn",
            detail="Could not determine Wi-Fi band.",
        )

    if "2.4" in band:
        return DiagnosticResult(
            check_id="wifi_band",
            title="Wi-Fi Band",
            status="warn",
            detail=f"Connected to {ssid} on 2.4GHz (Ch {channel}). 5GHz is faster and less congested.",
            fixable=False,
            fix_description="Switch to a 5GHz network if your router supports it.",
        )

    return DiagnosticResult(
        check_id="wifi_band",
        title="Wi-Fi Band",
        status="ok",
        detail=f"Connected to {ssid} on {band} (Ch {channel}) — good for speed.",
    )


def _check_signal_quality() -> DiagnosticResult:
    """Check Wi-Fi signal strength and SNR."""
    from probes import get_wifi_info
    wifi = get_wifi_info()
    snr = wifi.get("snr_db")
    rssi = wifi.get("rssi_dbm")

    if snr is None and rssi is None:
        return DiagnosticResult(
            check_id="signal_quality",
            title="Signal Quality",
            status="warn",
            detail="Could not read Wi-Fi signal metrics.",
        )

    if rssi is not None and rssi < -75:
        return DiagnosticResult(
            check_id="signal_quality",
            title="Signal Quality",
            status="fail",
            detail=f"Very weak signal ({rssi} dBm). You're too far from the router.",
            fixable=False,
            fix_description="Move closer to your Wi-Fi router, or add a Wi-Fi extender.",
        )

    if snr is not None and snr < 20:
        return DiagnosticResult(
            check_id="signal_quality",
            title="Signal Quality",
            status="warn",
            detail=f"Signal quality is marginal (SNR {snr} dB). Fine for audio, borderline for video.",
            fixable=False,
            fix_description="Reduce distance to router or remove obstacles between you and the router.",
        )

    quality = "Excellent" if (rssi and rssi >= -50) else "Good"
    return DiagnosticResult(
        check_id="signal_quality",
        title="Signal Quality",
        status="ok",
        detail=f"{quality} signal (RSSI {rssi} dBm, SNR {snr} dB).",
    )


def _check_proxy_or_vpn() -> DiagnosticResult:
    """Check if a web proxy or VPN is active (can add latency)."""
    # Check for HTTP proxy
    proxy_output = run_command(["networksetup", "-getwebproxy", "Wi-Fi"], timeout=5.0)
    proxy_enabled = "Enabled: Yes" in proxy_output

    # Check for VPN interfaces
    ifconfig = run_command(["ifconfig"], timeout=5.0)
    vpn_active = bool(re.search(r"(utun|ppp|tap|tun)\d+", ifconfig))

    if proxy_enabled:
        return DiagnosticResult(
            check_id="proxy_vpn",
            title="Proxy / VPN",
            status="warn",
            detail="Web proxy is enabled — this adds latency to all connections.",
            fixable=False,
            fix_description="Disable web proxy during calls if not required.",
        )
    if vpn_active:
        return DiagnosticResult(
            check_id="proxy_vpn",
            title="Proxy / VPN",
            status="warn",
            detail="VPN appears active — this can add latency and reduce speed.",
            fixable=False,
            fix_description="Disconnect VPN during video calls for better quality.",
        )
    return DiagnosticResult(
        check_id="proxy_vpn",
        title="Proxy / VPN",
        status="ok",
        detail="No proxy or VPN detected.",
    )


def _check_network_service_order() -> DiagnosticResult:
    """Check if Wi-Fi is the primary network service."""
    output = run_command(["networksetup", "-listnetworkserviceorder"], timeout=5.0)
    lines = output.strip().splitlines()
    for line in lines[:3]:
        if "Wi-Fi" in line or "AirPort" in line:
            return DiagnosticResult(
                check_id="service_order",
                title="Network Priority",
                status="ok",
                detail="Wi-Fi is the primary network service.",
            )
    return DiagnosticResult(
        check_id="service_order",
        title="Network Priority",
        status="warn",
        detail="Wi-Fi may not be the primary service. Other interfaces could cause routing issues.",
        fixable=False,
        fix_description="Set Wi-Fi as the primary network service in System Preferences.",
    )


def run_diagnostics(output_fn: Callable[[str], None] | None = None) -> list[DiagnosticResult]:
    """Run all diagnostic checks and return results."""
    checks = [
        ("Checking DNS servers...", _check_dns_servers),
        ("Testing DNS speed...", _check_dns_cache),
        ("Checking search domains...", _check_search_domains),
        ("Checking Wi-Fi band...", _check_wifi_band),
        ("Checking signal quality...", _check_signal_quality),
        ("Checking for VPN/proxy...", _check_proxy_or_vpn),
        ("Checking network priority...", _check_network_service_order),
    ]
    results: list[DiagnosticResult] = []
    for label, check_fn in checks:
        if output_fn:
            output_fn(label)
        try:
            results.append(check_fn())
        except Exception as e:
            results.append(
                DiagnosticResult(
                    check_id=check_fn.__name__,
                    title=label,
                    status="warn",
                    detail=f"Check failed: {e}",
                )
            )
    return results


def apply_fix(result: DiagnosticResult) -> tuple[bool, str]:
    """Apply the automated fix for a diagnostic result. Returns (success, message)."""
    if not result.fixable or result.fix_fn is None:
        return False, "No automated fix available for this issue."
    try:
        return result.fix_fn()
    except Exception as e:
        return False, f"Fix failed: {e}"
