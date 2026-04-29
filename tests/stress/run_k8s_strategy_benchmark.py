import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from load_spike import print_summary, run_load_test


def log(message: str) -> None:
    print(f"[k8s-benchmark] {message}", flush=True)


def run_kubectl(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    command = ["kubectl", *args]
    return subprocess.run(command, capture_output=True, text=True, check=check)


def fetch_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_configmap(namespace: str, name: str) -> dict:
    result = run_kubectl(["get", "configmap", name, "-n", namespace, "-o", "json"])
    return json.loads(result.stdout)


def patch_routing_strategy(namespace: str, configmap: str, strategy: str) -> None:
    log(f"patching ConfigMap {configmap} with ROUTING_STRATEGY={strategy}")
    payload = json.dumps({"data": {"ROUTING_STRATEGY": strategy}})
    run_kubectl(["patch", "configmap", configmap, "-n", namespace, "--type", "merge", "-p", payload])


def rollout_restart(namespace: str, deployment: str, timeout_seconds: int) -> None:
    log(f"restarting deployment/{deployment}")
    run_kubectl(["rollout", "restart", f"deployment/{deployment}", "-n", namespace])
    log(f"waiting for deployment/{deployment} rollout")
    run_kubectl(
        [
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n",
            namespace,
            f"--timeout={timeout_seconds}s",
        ]
    )


def start_port_forward(namespace: str, service: str, local_port: int, remote_port: int) -> subprocess.Popen:
    log(f"starting port-forward localhost:{local_port} -> svc/{service}:{remote_port}")
    log_file = tempfile.NamedTemporaryFile(
        mode="w+",
        encoding="utf-8",
        prefix="kubectl-port-forward-",
        suffix=".log",
        delete=False,
    )
    process = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, f"svc/{service}", f"{local_port}:{remote_port}"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process.log_path = log_file.name  # type: ignore[attr-defined]
    log_file.close()
    return process


def read_process_log(process: subprocess.Popen) -> str:
    log_path = getattr(process, "log_path", "")
    if not log_path:
        return ""
    try:
        return Path(log_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def stop_process(process: subprocess.Popen, label: str) -> str:
    if process.poll() is None:
        log(f"stopping {label}")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log(f"{label} did not exit on terminate, killing process")
            process.kill()
            process.wait(timeout=5)
    return read_process_log(process)


def wait_for_gateway(base_url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error = None
    log(f"waiting for gateway at {base_url}/healthz")
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "port-forward exited before gateway became reachable. "
                f"Output:\n{read_process_log(process)}"
            )
        try:
            data = fetch_json(f"{base_url}/healthz", timeout=2.0)
            if data.get("status") == "ok":
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.25)

    raise RuntimeError(
        f"gateway did not become reachable before timeout: {last_error}\n"
        f"Port-forward output:\n{read_process_log(process)}"
    )


def wait_for_strategy(base_url: str, expected_strategy: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_stats = None
    log(f"verifying in-cluster gateway strategy is {expected_strategy}")
    while time.time() < deadline:
        try:
            stats = fetch_json(f"{base_url}/stats", timeout=2.0)
            last_stats = stats
            actual = stats.get("router", {}).get("strategy")
            if actual == expected_strategy:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(
        f"gateway strategy did not match expected value {expected_strategy}. "
        f"Last observed stats: {last_stats}"
    )


def ensure_local_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"local port {port} is already in use; choose a different --local-port"
            ) from exc


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"writing benchmark report to {path}")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def write_markdown_summary(path: Path, report: dict) -> None:
    log(f"writing benchmark markdown summary to {path}")
    lines: list[str] = []
    lines.append("# Kubernetes Strategy Benchmark Summary")
    lines.append("")
    lines.append(f"- Generated at (UTC): `{report['generated_at_utc']}`")
    lines.append(f"- Namespace: `{report['namespace']}`")
    lines.append(f"- Gateway service: `{report['service']}`")
    lines.append(f"- Deployment: `{report['deployment']}`")
    lines.append(f"- Workload: `{report['workload']}`")
    lines.append(f"- Total requests: `{report['requests']}`")
    lines.append(f"- Concurrency: `{report['concurrency']}`")
    lines.append(f"- Prompt size: `{report['prompt_size']}`")
    lines.append(f"- Max tokens: `{report['max_tokens']}`")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Strategy | Req/sec | P50 ms | P95 ms | P99 ms | 200s | 429s | 503s | Other | Completed | Failed |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")

    for result in report["results"]:
        summary = result["load_summary"]
        status_counts = summary["status_counts"]
        ok = status_counts.get("200", status_counts.get(200, 0))
        too_many = status_counts.get("429", status_counts.get(429, 0))
        unavailable = status_counts.get("503", status_counts.get(503, 0))
        accounted = ok + too_many + unavailable
        other = sum(int(v) for v in status_counts.values()) - accounted
        stats_after = result["stats_after"]
        lines.append(
            f"| `{result['strategy']}` | "
            f"{summary['requests_per_second']:.2f} | "
            f"{summary['p50_latency_ms']:.2f} | "
            f"{summary['p95_latency_ms']:.2f} | "
            f"{summary['p99_latency_ms']:.2f} | "
            f"{ok} | {too_many} | {unavailable} | {other} | "
            f"{stats_after['completed_requests']} | {stats_after['failed_requests']} |"
        )

    lines.append("")
    lines.append("## Failure Reasons")
    lines.append("")
    for result in report["results"]:
        lines.append(f"### `{result['strategy']}`")
        lines.append("")
        for reason, count in sorted(result["load_summary"].get("failure_reasons", {}).items()):
            lines.append(f"- `{reason}`: `{count}`")
        lines.append("")

    lines.append("## Notes")
    lines.append("")
    lines.append("- This benchmark runs against the in-cluster gateway through `kubectl port-forward`.")
    lines.append("- The runner patches the gateway ConfigMap, restarts the gateway Deployment, and verifies the active strategy before sending load.")
    lines.append("- Timeout-heavy failures usually indicate tail-latency pressure rather than service crashes.")
    path.write_text("\n".join(lines), encoding="utf-8")


def print_comparison(report: dict) -> None:
    print(f"Kubernetes benchmark report written to {report['output_path']}")
    print()
    for result in report["results"]:
        print(f"Strategy: {result['strategy']}")
        print_summary(result["load_summary"])
        print()


def run_strategy(
    namespace: str,
    configmap: str,
    deployment: str,
    service: str,
    local_port: int,
    remote_port: int,
    strategy: str,
    requests: int,
    concurrency: int,
    prompt_size: int,
    max_tokens: int,
    timeout: float,
    request_timeout: int,
    warmup_seconds: float,
    rollout_timeout: int,
    workload: str,
) -> dict:
    patch_routing_strategy(namespace, configmap, strategy)
    rollout_restart(namespace, deployment, rollout_timeout)

    ensure_local_port_free(local_port)
    process = start_port_forward(namespace, service, local_port, remote_port)
    base_url = f"http://127.0.0.1:{local_port}"
    try:
        wait_for_gateway(base_url, process, timeout=15.0)
        wait_for_strategy(base_url, strategy, timeout=5.0)
        if warmup_seconds > 0:
            log(f"warming up for {warmup_seconds:.1f}s")
            time.sleep(warmup_seconds)

        log("capturing pre-run stats")
        before_stats = fetch_json(f"{base_url}/stats")
        log(
            f"starting load test: requests={requests} concurrency={concurrency} "
            f"prompt_size={prompt_size} max_tokens={max_tokens} workload={workload}"
        )
        summary = run_load_test(
            url=f"{base_url}/infer",
            requests=requests,
            concurrency=concurrency,
            prompt_size=prompt_size,
            max_tokens=max_tokens,
            timeout=timeout,
            workload=workload,
        )
        log("capturing post-run stats")
        after_stats = fetch_json(f"{base_url}/stats")
    finally:
        port_forward_log = stop_process(process, "port-forward")

    return {
        "strategy": strategy,
        "load_summary": summary,
        "stats_before": before_stats,
        "stats_after": after_stats,
        "port_forward_log": port_forward_log,
        "gateway_request_timeout_seconds": request_timeout,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Kubernetes gateway strategies by patching the in-cluster ConfigMap and restarting the gateway deployment.")
    parser.add_argument("--namespace", default="llm-sim")
    parser.add_argument("--configmap", default="gateway-config")
    parser.add_argument("--deployment", default="gateway")
    parser.add_argument("--service", default="gateway")
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--remote-port", type=int, default=8080)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--prompt-size", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--workload", choices=("uniform", "mixed"), default="mixed")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--request-timeout-seconds", type=int, default=20)
    parser.add_argument("--warmup-seconds", type=float, default=0.5)
    parser.add_argument("--rollout-timeout-seconds", type=int, default=120)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = Path(args.output) if args.output else repo_root / "benchmarks" / f"k8s-strategy-comparison-{timestamp}.json"
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    markdown_path = output_path.with_suffix(".md")

    original_config = get_configmap(args.namespace, args.configmap)
    original_strategy = original_config.get("data", {}).get("ROUTING_STRATEGY", "cost")

    report = {
        "generated_at_utc": timestamp,
        "output_path": str(output_path),
        "namespace": args.namespace,
        "configmap": args.configmap,
        "deployment": args.deployment,
        "service": args.service,
        "local_port": args.local_port,
        "remote_port": args.remote_port,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "prompt_size": args.prompt_size,
        "max_tokens": args.max_tokens,
        "workload": args.workload,
        "timeout_seconds": args.timeout,
        "gateway_request_timeout_seconds": args.request_timeout_seconds,
        "results": [],
    }

    try:
        for strategy in ("round_robin", "cost"):
            log(f"running Kubernetes strategy benchmark for {strategy}")
            result = run_strategy(
                namespace=args.namespace,
                configmap=args.configmap,
                deployment=args.deployment,
                service=args.service,
                local_port=args.local_port,
                remote_port=args.remote_port,
                strategy=strategy,
                requests=args.requests,
                concurrency=args.concurrency,
                prompt_size=args.prompt_size,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                request_timeout=args.request_timeout_seconds,
                warmup_seconds=args.warmup_seconds,
                rollout_timeout=args.rollout_timeout_seconds,
                workload=args.workload,
            )
            report["results"].append(result)
    finally:
        log(f"restoring original routing strategy: {original_strategy}")
        patch_routing_strategy(args.namespace, args.configmap, original_strategy)
        rollout_restart(args.namespace, args.deployment, args.rollout_timeout_seconds)

    write_report(output_path, report)
    write_markdown_summary(markdown_path, report)
    print_comparison(report)


if __name__ == "__main__":
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    main()
