import argparse
import itertools
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from run_k8s_strategy_benchmark import (
    ensure_local_port_free,
    fetch_json,
    run_kubectl,
    start_port_forward,
    stop_process,
    wait_for_gateway,
)


def log(message: str) -> None:
    print(f"[failure-drill] {message}", flush=True)


def send_request(url: str, request_id: int, timeout: float) -> dict:
    payload = json.dumps(
        {
            "prompt": "Explain how a scheduler handles a worker disappearing mid-request.",
            "max_tokens": 128,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": f"failure-drill-{request_id % 32:02d}",
        },
        method="POST",
    )

    started = time.perf_counter()
    status = 0
    worker_id = ""
    error = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = json.loads(response.read().decode("utf-8"))
            worker_id = body.get("worker_id", "")
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        error = "timeout" if "timed out" in str(exc).lower() else "transport_error"

    completed = time.perf_counter()
    return {
        "request_id": request_id,
        "started_monotonic": started,
        "completed_monotonic": completed,
        "latency_ms": (completed - started) * 1000,
        "status": status,
        "worker_id": worker_id,
        "error": error,
    }


def current_pod(namespace: str, label: str, excluded_name: str = "") -> dict | None:
    result = run_kubectl(["get", "pods", "-n", namespace, "-l", label, "-o", "json"])
    pods = json.loads(result.stdout).get("items", [])
    for pod in pods:
        name = pod.get("metadata", {}).get("name", "")
        if not name or name == excluded_name:
            continue
        if pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        return pod
    return None


def pod_is_ready(pod: dict) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
    )


def wait_for_replacement(
    namespace: str,
    label: str,
    deleted_name: str,
    timeout_seconds: float,
) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pod = current_pod(namespace, label, excluded_name=deleted_name)
        if pod and pod_is_ready(pod):
            return pod
        time.sleep(0.5)
    raise RuntimeError("replacement worker pod did not become ready before timeout")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def summarize(events: list[dict]) -> dict:
    statuses: dict[str, int] = {}
    workers: dict[str, int] = {}
    successful_latencies: list[float] = []
    for event in events:
        status = str(event["status"])
        statuses[status] = statuses.get(status, 0) + 1
        if event["worker_id"]:
            workers[event["worker_id"]] = workers.get(event["worker_id"], 0) + 1
        if event["status"] == 200:
            successful_latencies.append(event["latency_ms"])
    return {
        "requests": len(events),
        "successful": len(successful_latencies),
        "status_counts": statuses,
        "worker_counts": workers,
        "p50_success_latency_ms": percentile(successful_latencies, 0.50),
        "p95_success_latency_ms": percentile(successful_latencies, 0.95),
        "p99_success_latency_ms": percentile(successful_latencies, 0.99),
    }


def write_markdown(path: Path, report: dict) -> None:
    before = report["windows"]["before_deletion"]
    unavailable = report["windows"]["worker_unavailable"]
    after = report["windows"]["after_recovery"]
    lines = [
        "# Kubernetes Worker Failure Drill",
        "",
        "This experiment sent continuous traffic through the gateway while Kubernetes deleted and replaced one worker pod.",
        "",
        "## Methodology",
        "",
        f"- Generated at (UTC): `{report['generated_at_utc']}`",
        f"- Namespace: `{report['namespace']}`",
        f"- Deleted pod: `{report['deleted_pod']}`",
        f"- Replacement pod: `{report['replacement_pod']}`",
        f"- Traffic concurrency: `{report['concurrency']}`",
        f"- Measured replacement readiness: `{report['replacement_ready_seconds']:.2f}` seconds",
        "- Synthetic API-key identifiers were rotated to keep rate limiting from dominating the drill.",
        "",
        "## Results",
        "",
        "| Window | Requests | Successful | P50 ms | P95 ms | P99 ms | Worker distribution |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        format_window("Before deletion", before),
        format_window("Worker unavailable", unavailable),
        format_window("After recovery", after),
        "",
        "## Interpretation",
        "",
        "The unavailable window covers requests that started after pod deletion and before the replacement pod became ready. Successful requests in that window demonstrate continuity through the remaining workers; non-200 responses show the user-visible disruption during recovery.",
        "",
        "This is a simulator result from one local Kubernetes environment. It does not establish production availability or real-model performance.",
        "",
        "The accompanying JSON contains every request event plus gateway snapshots taken before deletion and after recovery.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def format_window(label: str, window: dict) -> str:
    workers = ", ".join(
        f"{worker}: {count}" for worker, count in sorted(window["worker_counts"].items())
    ) or "none"
    return (
        f"| {label} | {window['requests']} | {window['successful']} | "
        f"{window['p50_success_latency_ms']:.2f} | {window['p95_success_latency_ms']:.2f} | "
        f"{window['p99_success_latency_ms']:.2f} | {workers} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure gateway behavior while Kubernetes replaces one worker pod."
    )
    parser.add_argument("--namespace", default="llm-sim")
    parser.add_argument("--target-label", default="app=worker-b")
    parser.add_argument("--service", default="gateway")
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--remote-port", type=int, default=8080)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--baseline-seconds", type=float, default=5.0)
    parser.add_argument("--post-recovery-seconds", type=float, default=10.0)
    parser.add_argument("--recovery-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=25.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if args.concurrency <= 0:
        parser.error("--concurrency must be positive")

    repo_root = Path(__file__).resolve().parents[2]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = (
        Path(args.output)
        if args.output
        else repo_root / "benchmarks" / f"k8s-failure-drill-{timestamp}.json"
    )
    if not output_path.is_absolute():
        output_path = repo_root / output_path

    target = current_pod(args.namespace, args.target_label)
    if not target or not pod_is_ready(target):
        raise RuntimeError("target worker pod is missing or not ready")
    deleted_name = target["metadata"]["name"]

    ensure_local_port_free(args.local_port)
    port_forward = start_port_forward(
        args.namespace, args.service, args.local_port, args.remote_port
    )
    base_url = f"http://127.0.0.1:{args.local_port}"
    events: list[dict] = []
    events_lock = threading.Lock()
    stop_traffic = threading.Event()
    counter = itertools.count()
    experiment_started = time.perf_counter()
    executor = ThreadPoolExecutor(max_workers=args.concurrency)
    futures = []

    def traffic_worker() -> None:
        while not stop_traffic.is_set():
            event = send_request(
                f"{base_url}/infer",
                next(counter),
                args.request_timeout_seconds,
            )
            with events_lock:
                events.append(event)

    try:
        wait_for_gateway(base_url, port_forward, timeout=15.0)
        stats_before = fetch_json(f"{base_url}/stats")
        futures = [executor.submit(traffic_worker) for _ in range(args.concurrency)]
        log(f"collecting {args.baseline_seconds:.1f}s baseline")
        time.sleep(args.baseline_seconds)

        deletion_started = time.perf_counter()
        log(f"deleting pod/{deleted_name}")
        run_kubectl(
            ["delete", "pod", deleted_name, "-n", args.namespace, "--wait=false"]
        )
        replacement = wait_for_replacement(
            args.namespace,
            args.target_label,
            deleted_name,
            args.recovery_timeout_seconds,
        )
        replacement_ready = time.perf_counter()
        replacement_name = replacement["metadata"]["name"]
        log(
            f"replacement pod/{replacement_name} ready after "
            f"{replacement_ready - deletion_started:.2f}s"
        )
        time.sleep(args.post_recovery_seconds)
        stop_traffic.set()
        for future in futures:
            future.result()

        stats_after = fetch_json(f"{base_url}/stats")
    finally:
        stop_traffic.set()
        executor.shutdown(wait=True)
        port_forward_log = stop_process(port_forward, "port-forward")

    for event in events:
        event["started_seconds"] = event.pop("started_monotonic") - experiment_started
        event["completed_seconds"] = event.pop("completed_monotonic") - experiment_started

    deletion_seconds = deletion_started - experiment_started
    replacement_seconds = replacement_ready - experiment_started
    before_events = [event for event in events if event["started_seconds"] < deletion_seconds]
    unavailable_events = [
        event
        for event in events
        if deletion_seconds <= event["started_seconds"] < replacement_seconds
    ]
    after_events = [
        event for event in events if event["started_seconds"] >= replacement_seconds
    ]

    report = {
        "generated_at_utc": timestamp,
        "namespace": args.namespace,
        "target_label": args.target_label,
        "deleted_pod": deleted_name,
        "replacement_pod": replacement_name,
        "concurrency": args.concurrency,
        "baseline_seconds": args.baseline_seconds,
        "post_recovery_seconds": args.post_recovery_seconds,
        "replacement_ready_seconds": replacement_ready - deletion_started,
        "windows": {
            "before_deletion": summarize(before_events),
            "worker_unavailable": summarize(unavailable_events),
            "after_recovery": summarize(after_events),
        },
        "stats_before": stats_before,
        "stats_after": stats_after,
        "events": sorted(events, key=lambda event: event["request_id"]),
        "port_forward_log": port_forward_log,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_markdown(output_path.with_suffix(".md"), report)
    log(f"wrote {output_path} and {output_path.with_suffix('.md')}")


if __name__ == "__main__":
    main()
