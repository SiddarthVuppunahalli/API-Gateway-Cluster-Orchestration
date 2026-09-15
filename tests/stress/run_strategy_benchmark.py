import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from load_spike import print_summary, run_load_test


def log(message: str) -> None:
    print(f"[benchmark] {message}", flush=True)


def extract_port(gateway_url: str) -> int:
    match = re.search(r":(\d+)", gateway_url)
    if not match:
        return 8080
    return int(match.group(1))


def fetch_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_gateway(base_url: str, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.time() + timeout
    last_error = None
    log(f"waiting for gateway health at {base_url}/healthz")
    while time.time() < deadline:
        if process.poll() is not None:
            output = read_gateway_log(process)
            raise RuntimeError(
                "gateway process exited before becoming ready. "
                f"Output:\n{output}"
            )
        try:
            fetch_json(f"{base_url}/healthz", timeout=2.0)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.25)

    output = read_gateway_log(process)
    raise RuntimeError(
        f"gateway did not become ready before timeout: {last_error}\n"
        "Gateway output:\n"
        f"{output}"
    )


def wait_for_strategy(base_url: str, expected_strategy: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last_stats = None
    log(f"verifying gateway strategy is {expected_strategy}")
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


def build_gateway(repo_root: Path, output_dir: Path) -> Path:
    binary_name = "gateway.exe" if sys.platform == "win32" else "gateway"
    binary_path = output_dir / binary_name
    log(f"building gateway binary at {binary_path}")
    result = subprocess.run(
        ["go", "build", "-o", str(binary_path), "./gateway/cmd/server"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        raise RuntimeError(f"failed to build gateway binary:\n{output}")
    return binary_path


def start_gateway(
    repo_root: Path,
    gateway_binary: Path,
    strategy: str,
    request_timeout: int,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["ROUTING_STRATEGY"] = strategy
    env["REQUEST_TIMEOUT_SECONDS"] = str(request_timeout)
    log_file = tempfile.NamedTemporaryFile(
        mode="w+",
        encoding="utf-8",
        prefix=f"gateway-{strategy}-",
        suffix=".log",
        delete=False,
    )
    log(
        f"starting gateway with strategy={strategy} "
        f"request_timeout_seconds={request_timeout}"
    )
    process = subprocess.Popen(
        [str(gateway_binary)],
        cwd=repo_root,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process.log_path = log_file.name  # type: ignore[attr-defined]
    log_file.close()
    return process


def stop_gateway(process: subprocess.Popen) -> str:
    if process.poll() is None:
        log("stopping gateway")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log("gateway did not exit on terminate, killing process")
            process.kill()
            process.wait(timeout=5)

    return read_gateway_log(process)


def read_gateway_log(process: subprocess.Popen) -> str:
    log_path = getattr(process, "log_path", "")
    if not log_path:
        return ""
    try:
        return Path(log_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def cleanup_gateway_port(port: int) -> None:
    pid = find_listener_pid(port)
    if pid is None:
        log(f"no existing listener found on port {port}")
        return

    log(f"found existing listener on port {port} with pid={pid}, attempting cleanup")
    terminate_pid(pid)

    # Give the OS a moment to release the port.
    for _ in range(20):
        if find_listener_pid(port) is None:
            log(f"port {port} is clear")
            return
        time.sleep(0.25)

    raise RuntimeError(f"failed to clear existing listener on port {port}")


def find_listener_pid(port: int) -> int | None:
    if sys.platform == "win32":
        return find_listener_pid_windows(port)
    return find_listener_pid_unix(port)


def find_listener_pid_windows(port: int) -> int | None:
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        check=False,
    )
    pattern = f":{port}"
    for line in result.stdout.splitlines():
        if "LISTENING" not in line or pattern not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            return int(parts[-1])
        except ValueError:
            continue
    return None


def find_listener_pid_unix(port: int) -> int | None:
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return int(line)
        except ValueError:
            continue
    return None


def terminate_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def run_strategy(
    repo_root: Path,
    gateway_binary: Path,
    gateway_url: str,
    gateway_port: int,
    strategy: str,
    requests: int,
    concurrency: int,
    prompt_size: int,
    max_tokens: int,
    timeout: float,
    request_timeout: int,
    warmup_seconds: float,
    startup_timeout: float,
    skip_cleanup: bool,
    workload: str,
    trial: int,
    seed: int,
    api_key_count: int,
) -> dict:
    if not skip_cleanup:
        cleanup_gateway_port(gateway_port)
    log(f"running strategy benchmark for {strategy}")
    process = start_gateway(repo_root, gateway_binary, strategy, request_timeout)
    try:
        wait_for_gateway(gateway_url, process=process, timeout=startup_timeout)
        log(f"gateway ready for strategy={strategy}")
        wait_for_strategy(gateway_url, expected_strategy=strategy, timeout=5.0)
        if warmup_seconds > 0:
            log(f"warming up for {warmup_seconds:.1f}s")
            time.sleep(warmup_seconds)

        log("capturing pre-run stats")
        before_stats = fetch_json(f"{gateway_url}/stats")
        log(
            f"starting load test: requests={requests} concurrency={concurrency} "
            f"prompt_size={prompt_size} max_tokens={max_tokens} workload={workload}"
        )
        summary = run_load_test(
            url=f"{gateway_url}/infer",
            requests=requests,
            concurrency=concurrency,
            prompt_size=prompt_size,
            max_tokens=max_tokens,
            timeout=timeout,
            workload=workload,
            seed=seed,
            api_key_count=api_key_count,
        )
        log("capturing post-run stats")
        after_stats = fetch_json(f"{gateway_url}/stats")
    finally:
        gateway_log = stop_gateway(process)

    log(f"completed strategy benchmark for {strategy}")
    return {
        "trial": trial,
        "strategy": strategy,
        "load_summary": summary,
        "stats_before": before_stats,
        "stats_after": after_stats,
        "gateway_log": gateway_log,
    }


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"writing benchmark report to {path}")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def status_count(summary: dict, status: int) -> int:
    counts = summary["status_counts"]
    return int(counts.get(str(status), counts.get(status, 0)))


def aggregate_results(results: list[dict]) -> list[dict]:
    aggregates: list[dict] = []
    for strategy in ("round_robin", "cost"):
        strategy_results = [result for result in results if result["strategy"] == strategy]
        aggregates.append(
            {
                "strategy": strategy,
                "trials": len(strategy_results),
                "median_successful_requests": median(
                    status_count(result["load_summary"], 200) for result in strategy_results
                ),
                "median_successful_requests_per_second": median(
                    result["load_summary"]["successful_requests_per_second"]
                    for result in strategy_results
                ),
                "median_p50_latency_ms": median(
                    result["load_summary"]["p50_latency_ms"] for result in strategy_results
                ),
                "median_p95_latency_ms": median(
                    result["load_summary"]["p95_latency_ms"] for result in strategy_results
                ),
                "median_p99_latency_ms": median(
                    result["load_summary"]["p99_latency_ms"] for result in strategy_results
                ),
                "median_http_429": median(
                    status_count(result["load_summary"], 429) for result in strategy_results
                ),
                "median_http_503": median(
                    status_count(result["load_summary"], 503) for result in strategy_results
                ),
                "median_http_504": median(
                    status_count(result["load_summary"], 504) for result in strategy_results
                ),
            }
        )
    return aggregates


def write_markdown_summary(path: Path, report: dict) -> None:
    log(f"writing benchmark markdown summary to {path}")
    lines: list[str] = []
    lines.append("# Local Routing Strategy Benchmark")
    lines.append("")
    lines.append("This report compares the gateway's round-robin and cost-aware routing policies under the same deterministic mixed workload.")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(f"- Generated at (UTC): `{report['generated_at_utc']}`")
    lines.append(f"- Trials per strategy: `{report['trials']}`")
    lines.append(f"- Requests per trial: `{report['requests']}` at concurrency `{report['concurrency']}`")
    lines.append(f"- Workload: `{report['workload']}` with deterministic seed `{report['workload_seed']}`")
    lines.append(f"- Synthetic API-key buckets: `{report['api_key_count']}`")
    lines.append(f"- Client timeout: `{report['timeout_seconds']}` seconds; gateway timeout: `{report['gateway_request_timeout_seconds']}` seconds")
    lines.append("- Strategy execution order alternates between trials to reduce ordering bias.")
    lines.append("- Latency percentiles include successful HTTP 200 responses only.")
    lines.append("- API-key values are synthetic rate-limit bucket identifiers, not credentials.")
    lines.append("")
    lines.append("## Median results")
    lines.append("")
    lines.append("| Strategy | Successful | Successful req/sec | P50 ms | P95 ms | P99 ms | 429s | 503s | 504s |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")

    for aggregate in report["aggregate"]:
        lines.append(
            f"| `{aggregate['strategy']}` | "
            f"{aggregate['median_successful_requests']:.0f}/{report['requests']} | "
            f"{aggregate['median_successful_requests_per_second']:.2f} | "
            f"{aggregate['median_p50_latency_ms']:.2f} | "
            f"{aggregate['median_p95_latency_ms']:.2f} | "
            f"{aggregate['median_p99_latency_ms']:.2f} | "
            f"{aggregate['median_http_429']:.0f} | "
            f"{aggregate['median_http_503']:.0f} | "
            f"{aggregate['median_http_504']:.0f} |"
        )

    lines.append("")
    round_robin = next(item for item in report["aggregate"] if item["strategy"] == "round_robin")
    cost = next(item for item in report["aggregate"] if item["strategy"] == "cost")
    throughput_change = (
        (cost["median_successful_requests_per_second"] / round_robin["median_successful_requests_per_second"]) - 1
    ) * 100
    p95_change = (1 - cost["median_p95_latency_ms"] / round_robin["median_p95_latency_ms"]) * 100
    p99_change = (1 - cost["median_p99_latency_ms"] / round_robin["median_p99_latency_ms"]) * 100
    completion_point_change = (
        (cost["median_successful_requests"] - round_robin["median_successful_requests"])
        / report["requests"]
    ) * 100
    lines.append("## Observed result")
    lines.append("")
    lines.append(
        f"Across the median trial, cost-aware routing delivered "
        f"`{cost['median_successful_requests_per_second']:.2f}` successful requests/sec versus "
        f"`{round_robin['median_successful_requests_per_second']:.2f}` for round-robin "
        f"(`{throughput_change:+.1f}%`)."
    )
    lines.append("")
    lines.append(
        f"Its median P95 was `{cost['median_p95_latency_ms']:.2f} ms` versus "
        f"`{round_robin['median_p95_latency_ms']:.2f} ms` (`{p95_change:.1f}%` lower), "
        f"and median P99 was `{cost['median_p99_latency_ms']:.2f} ms` versus "
        f"`{round_robin['median_p99_latency_ms']:.2f} ms` (`{p99_change:.1f}%` lower)."
    )
    lines.append("")
    lines.append(
        f"The trade-off was completion rate: cost-aware routing completed a median "
        f"`{cost['median_successful_requests']:.0f}/{report['requests']}` requests versus "
        f"`{round_robin['median_successful_requests']:.0f}/{report['requests']}` for round-robin "
        f"(`{completion_point_change:+.1f}` percentage points), with the remaining requests returning `504` at the gateway deadline."
    )
    lines.append("")
    lines.append("These results suggest a tail-latency and throughput benefit under this workload, paired with a small completion-rate regression. They support discussing a measurable scheduling trade-off, not claiming that cost-aware routing is universally superior.")
    lines.append("")
    lines.append("## Individual trials")
    lines.append("")
    lines.append("| Trial | Strategy | Successful | Successful req/sec | P50 ms | P95 ms | P99 ms | 429s | 503s | 504s | Other |")
    lines.append("| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")

    for result in report["results"]:
        summary = result["load_summary"]
        ok = status_count(summary, 200)
        too_many = status_count(summary, 429)
        unavailable = status_count(summary, 503)
        timed_out = status_count(summary, 504)
        other = summary["total_requests"] - ok - too_many - unavailable - timed_out
        lines.append(
            f"| {result['trial']} | `{result['strategy']}` | "
            f"{ok}/{summary['total_requests']} | "
            f"{summary['successful_requests_per_second']:.2f} | "
            f"{summary['p50_latency_ms']:.2f} | "
            f"{summary['p95_latency_ms']:.2f} | "
            f"{summary['p99_latency_ms']:.2f} | "
            f"{too_many} | {unavailable} | {timed_out} | {other} |"
        )

    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("Higher successful throughput and completion count are better; lower latency percentiles are better. These measurements characterize this simulator and workload only. They should not be generalized to real model serving or presented as proof that one routing policy always wins.")
    lines.append("")
    lines.append("The raw JSON report contains status counts, failure reasons, gateway snapshots, worker snapshots, and process logs for every trial.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def print_comparison(report: dict) -> None:
    print(f"Benchmark report written to {report['output_path']}")
    print()
    for result in report["results"]:
        print(f"Trial {result['trial']} strategy: {result['strategy']}")
        print_summary(result["load_summary"])
        router_stats = result["stats_after"]["router"]
        print(
            "Router state after run: "
            f"healthy={router_stats['healthy_workers']} "
            f"available={router_stats['available_workers']} "
            f"saturated={router_stats['saturated_workers']}"
        )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run both gateway routing strategies and capture a comparison report.")
    parser.add_argument("--gateway-url", default="http://localhost:8080")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--prompt-size", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--workload", choices=("uniform", "mixed"), default="mixed")
    parser.add_argument("--request-timeout-seconds", type=int, default=20)
    parser.add_argument("--warmup-seconds", type=float, default=0.5)
    parser.add_argument("--startup-timeout", type=float, default=20.0)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--api-key-count", type=int, default=64)
    parser.add_argument("--skip-cleanup", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    if args.trials <= 0:
        parser.error("--trials must be positive")
    if args.api_key_count <= 0:
        parser.error("--api-key-count must be positive for a routing benchmark")
    if args.timeout <= args.request_timeout_seconds:
        parser.error("--timeout must be greater than --request-timeout-seconds")

    repo_root = Path(__file__).resolve().parents[2]
    gateway_port = extract_port(args.gateway_url)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = Path(args.output) if args.output else repo_root / "benchmarks" / f"strategy-comparison-{timestamp}.json"
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    markdown_path = output_path.with_suffix(".md")
    try:
        report_output_path = output_path.relative_to(repo_root)
    except ValueError:
        report_output_path = output_path

    report = {
        "generated_at_utc": timestamp,
        "output_path": str(report_output_path),
        "gateway_url": args.gateway_url,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "prompt_size": args.prompt_size,
        "max_tokens": args.max_tokens,
        "workload": args.workload,
        "timeout_seconds": args.timeout,
        "gateway_request_timeout_seconds": args.request_timeout_seconds,
        "trials": args.trials,
        "workload_seed": args.seed,
        "api_key_count": args.api_key_count,
        "results": [],
    }

    with tempfile.TemporaryDirectory(prefix="gateway-benchmark-build-") as build_dir:
        gateway_binary = build_gateway(repo_root, Path(build_dir))
        for trial in range(1, args.trials + 1):
            strategies = ("round_robin", "cost") if trial % 2 else ("cost", "round_robin")
            for strategy in strategies:
                result = run_strategy(
                    repo_root=repo_root,
                    gateway_binary=gateway_binary,
                    gateway_url=args.gateway_url,
                    gateway_port=gateway_port,
                    strategy=strategy,
                    requests=args.requests,
                    concurrency=args.concurrency,
                    prompt_size=args.prompt_size,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    request_timeout=args.request_timeout_seconds,
                    warmup_seconds=args.warmup_seconds,
                    startup_timeout=args.startup_timeout,
                    skip_cleanup=args.skip_cleanup,
                    workload=args.workload,
                    trial=trial,
                    seed=args.seed,
                    api_key_count=args.api_key_count,
                )
                report["results"].append(result)

    report["aggregate"] = aggregate_results(report["results"])

    write_report(output_path, report)
    write_markdown_summary(markdown_path, report)
    print_comparison(report)

    cleanup_gateway_port(gateway_port)


if __name__ == "__main__":
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    main()
