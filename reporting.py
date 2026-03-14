from __future__ import annotations

from typing import Any

from doctor import get_friendly_name, get_impact_description, get_friendly_recommendation


PROFILE_LABELS = {
    "audio": "Audio call",
    "video": "Video call",
    "video_share": "Video + Screen-share call",
}


METRIC_LABELS = {
    "latency_p50_ms": "Typical Delay (ms)",
    "latency_p95_ms": "Call Delay (ms)",
    "jitter_ms": "Voice Stability (ms)",
    "packet_loss_pct": "Dropped Audio/Video (%)",
    "secondary_packet_loss_pct": "Backup Route Reliability (%)",
    "secondary_latency_p95_ms": "Backup Route Delay (ms)",
    "gateway_packet_loss_pct": "Router Connection Drops (%)",
    "gateway_latency_p95_ms": "Router Response Time (ms)",
    "spike_count": "Lag Spikes",
    "dropout_count": "Total Dropouts",
    "snr_db": "Signal Quality (dB)",
    "rssi_dbm": "Signal Strength (dBm)",
    "noise_dbm": "Background Interference (dBm)",
    "tx_rate_mbps": "Wi-Fi Link Speed (Mbps)",
    "dns_p95_ms": "App & Page Load Speed (ms)",
    "dns_failures": "Failed Website Lookups",
    "route_timeout_hops": "Network Path Health",
    "congestion_delta_ms": "Slowdown Under Load (ms)",
    "download_mbps": "Download Speed (Mbps)",
    "upload_mbps": "Upload Speed (Mbps)",
}


def _format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _issue_line(issue: dict[str, Any]) -> str:
    metric = issue.get("metric", "")
    severity = issue.get("severity", "WARN")
    friendly = get_friendly_name(metric)
    impact = get_impact_description(metric, severity)
    return f"- [{severity}] {friendly}: {impact}"


def _format_use_case_results(use_case_results: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for key in ("audio", "video", "video_share"):
        result = use_case_results.get(key)
        if not result:
            continue
        lines.append(f"- {PROFILE_LABELS.get(key, key)}: {result.get('verdict', 'n/a')}")
    return lines


def format_report(report: dict[str, Any]) -> str:
    profile = report.get("profile", "video")
    profile_name = PROFILE_LABELS.get(profile, profile)
    profile_result = report.get("profile_result", {})
    metrics = report.get("metrics", {})
    issues = profile_result.get("issues", [])
    overall_verdict = report.get("overall_verdict", profile_result.get("verdict", "WARN"))
    use_case_results = report.get("use_case_results", {})
    wifi_performance = report.get("wifi_performance", {})

    lines: list[str] = []
    lines.append("Vibe Check WiFi")
    lines.append(f"Overall verdict: {overall_verdict}")
    lines.append(f"Profile verdict: {profile_result.get('verdict', 'WARN')} ({profile})")
    lines.append(f"Profile type: {profile_name}")

    lines.append("")
    lines.append("Connection speed")
    speed = report.get("speed_test")
    if speed:
        if speed.get("error"):
            lines.append(f"- Could not complete speed test: {speed['error']}")
        else:
            lines.append(f"- Download (Mbps): {_format_value(speed.get('download_mbps'))}")
            lines.append(f"- Upload (Mbps): {_format_value(speed.get('upload_mbps'))}")
    else:
        lines.append("- Not measured in this run.")

    lines.append("")
    lines.append("Wi-Fi performance")
    if wifi_performance:
        lines.append(f"- Rating: {wifi_performance.get('rating', 'Unknown')}")
        summary = wifi_performance.get("summary")
        if summary:
            lines.append(f"- {summary}")
    else:
        lines.append("- No Wi-Fi performance summary available.")

    lines.append("")
    lines.append("Use-case readiness")
    readiness_lines = _format_use_case_results(use_case_results)
    if readiness_lines:
        lines.extend(readiness_lines)
    else:
        lines.append("- No use-case readiness summary available.")

    lines.append("")
    lines.append("Top problems detected")
    if issues:
        for issue in issues[:5]:
            lines.append(_issue_line(issue))
    else:
        lines.append("- No major issues detected for this profile.")

    lines.append("")
    lines.append("Detailed metrics")
    for key, label in METRIC_LABELS.items():
        if key in metrics:
            lines.append(f"- {label}: {_format_value(metrics[key])}")

    lines.append("")
    lines.append("What to do next")
    if issues:
        seen: set[str] = set()
        for issue in issues:
            metric = issue.get("metric", "")
            severity = issue.get("severity", "WARN")
            # Use friendly recommendation first, fall back to original
            friendly_rec = get_friendly_recommendation(metric, severity)
            recommendation = friendly_rec or issue.get("recommendation", "")
            if recommendation and recommendation not in seen:
                lines.append(f"- {recommendation}")
                seen.add(recommendation)
    else:
        lines.append("- Connection looks stable for this profile right now.")
        lines.append("- Keep live monitor running during calls to catch intermittent spikes.")

    return "\n".join(lines)
