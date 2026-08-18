"""Run a reproducible local-model A/B benchmark through the real Unity agent CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


ROOT = Path(__file__).resolve().parent
MODELS = {
    "A": "qwen3-coder:30b",
    "B": "qwen3.6:35b-a3b-q4_K_M",
}
TASKS = ("movement_jump", "camera_follow", "long_composite")
TASK_CODES = {
    "movement_jump": "MJ",
    "camera_follow": "CF",
    "long_composite": "LC",
}


def _replace_first_scene(text: str, scene: str) -> str:
    start = text.find("Assets/Scenes/")
    end = text.find(".unity", start)
    if start < 0 or end < 0:
        raise ValueError("base prompt has no scene path")
    return text[:start] + scene + text[end + len(".unity"):]


def make_prompt(task: str, run_id: str) -> str:
    scene = f"Assets/Scenes/ModelBenchmark/Scored/{run_id}.unity"
    move_class = f"{run_id}Movement"
    move_path = f"Assets/Scripts/ModelBenchmark/{move_class}.cs"

    if task == "movement_jump":
        text = (ROOT / "prompts/platformer_movement_jump_v11111_clean7.txt").read_text(
            encoding="utf-8"
        )
        text = _replace_first_scene(text, scene)
        return text.rstrip() + (
            f"\n이 실행의 이동 스크립트는 {move_path}에 작성하고 클래스명은 "
            f"{move_class}로 파일명과 일치시켜라. 다른 기존 이동 스크립트를 읽거나 "
            "수정하지 마라.\n"
        )

    if task == "camera_follow":
        text = (ROOT / "prompts/camera_follow_repro8.txt").read_text(encoding="utf-8")
        text = _replace_first_scene(text, scene)
        return text.rstrip() + (
            f"\n이 실행에서 새로 만드는 스크립트의 파일명과 클래스명에는 고유 접두사 "
            f"{run_id}를 사용해 서로 일치시켜라. 다른 기존 스크립트를 읽거나 수정하지 "
            "마라.\n"
        )

    if task == "long_composite":
        camera_class = f"{run_id}Camera"
        camera_path = f"Assets/Scripts/ModelBenchmark/{camera_class}.cs"
        text = (ROOT / "prompts/v112_repro20.txt").read_text(encoding="utf-8")
        text = text.replace("Assets/Scenes/V112Repro20.unity", scene)
        text = text.replace("Assets/Scripts/PlayerMovement25D.cs", move_path)
        text = text.replace("Assets/Scripts/SideScrollerCamera.cs", camera_path)
        text = text.replace("PlayerMovement25D", move_class)
        text = text.replace("SideScrollerCamera", camera_class)
        return text

    raise ValueError(f"unknown task: {task}")


def warm_model(model: str) -> float:
    started = time.monotonic()
    body = json.dumps({
        "model": model,
        "prompt": "",
        "stream": False,
        "keep_alive": "30m",
        "options": {"num_ctx": 32768, "num_predict": 1},
    }).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        response.read()
    return round(time.monotonic() - started, 3)


async def cleanup_unity() -> str:
    """Post-run cleanup only; never contributes to verification results."""
    from mcp_client import UnityTools

    async with UnityTools() as tools:
        state = await tools.call("unity_get_state", {})
        if '"isPlaying":true' in state:
            await tools.call("unity_release_all_keys", {})
            await tools.call("unity_play_mode", {"action": "stop"})
            return "stopped_play_mode"
        return "already_idle"


def dated_json_files(root: Path) -> set[Path]:
    return set(root.glob("*/*/*/*.json")) if root.exists() else set()


def dated_jsonl_files(root: Path) -> set[Path]:
    return set(root.glob("*/*/*/*.jsonl")) if root.exists() else set()


def read_jsonl_metrics(path: Path | None) -> dict:
    metrics = {
        "model_calls": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "leaked_tool_call_warnings": 0,
        "iteration_limit_events": 0,
        "policy_block_events": 0,
    }
    if path is None or not path.exists():
        return metrics
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("event")
        if kind == "assistant_response":
            metrics["model_calls"] += 1
            metrics["tool_calls"] += len(event.get("tool_calls") or [])
        elif kind == "tool_result":
            metrics["tool_results"] += 1
            if "Policy blocked" in str(event.get("result", "")):
                metrics["policy_block_events"] += 1
        elif kind == "warning":
            message = str(event.get("message", ""))
            if "tool-call" in message and "복구" in message:
                metrics["leaked_tool_call_warnings"] += 1
            if "iteration limit" in message.lower() or "반복 한도" in message:
                metrics["iteration_limit_events"] += 1
    return metrics


def receipt_metrics(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {
            "status": "no_receipt",
            "build_stage_success": None,
            "attempt_count": 0,
            "requested_count": 0,
            "measured_count": 0,
            "skipped_count": 0,
            "unmapped_count": 0,
            "receipt_elapsed_seconds": None,
            "failure_codes": [],
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    failures = []
    for attempt in data.get("attempts") or []:
        failures.extend(str(item) for item in (attempt.get("failures") or []))
    return {
        "status": data.get("status"),
        "build_stage_success": data.get("build_stage_success"),
        "attempt_count": len(data.get("attempts") or []),
        "requested_count": len(data.get("requested_checks") or []),
        "measured_count": len(data.get("measured_checks") or []),
        "skipped_count": len(data.get("skipped_checks") or []),
        "unmapped_count": len(data.get("unmapped_requirements") or []),
        "receipt_elapsed_seconds": data.get("elapsed_seconds"),
        "failure_codes": failures,
    }


def newest_added(before: set[Path], after: set[Path]) -> Path | None:
    added = after - before
    return max(added, key=lambda path: path.stat().st_mtime) if added else None


def run_one(
    *, project: Path, output_dir: Path, task: str, repetition: int,
    variant: str, model_key: str, id_suffix: str = "",
) -> dict:
    model = MODELS[model_key]
    run_id = f"MB{TASK_CODES[task]}{repetition:02d}{variant}{id_suffix}"
    prompt_path = output_dir / "prompts" / f"{run_id}.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(make_prompt(task, run_id), encoding="utf-8")

    print(f"RUN_START {run_id} task={task} model={model}", flush=True)
    for installed_model in MODELS.values():
        subprocess.run(
            ["ollama", "stop", installed_model],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    warm_seconds = warm_model(model)
    print(f"MODEL_READY {run_id} warm_seconds={warm_seconds}", flush=True)

    receipts_root = ROOT / "logs/receipts"
    runs_root = ROOT / "logs/runs"
    receipts_before = dated_json_files(receipts_root)
    runs_before = dated_jsonl_files(runs_root)

    stdout_path = output_dir / "stdout" / f"{run_id}.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "UNITY_AGENT_MODEL": model,
        "UNITY_AGENT_NUM_CTX": "32768",
        "UNITY_AGENT_TEMPERATURE": "0.2",
        "UNITY_AGENT_AUTO_VISION": "0",
        "UNITY_AGENT_TASK_TIMEOUT": "600",
        "PYTHONUNBUFFERED": "1",
    })
    command = [
        "uv", "run", "python", "main.py", "--project", str(project),
        "--prompt-file", str(prompt_path),
    ]
    started = time.monotonic()
    with stdout_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
            text=True,
        )
        next_heartbeat = started + 30
        while process.poll() is None:
            now = time.monotonic()
            if now >= next_heartbeat:
                size = stdout_path.stat().st_size if stdout_path.exists() else 0
                print(
                    f"RUN_ACTIVE {run_id} elapsed={round(now-started)}s stdout_bytes={size}",
                    flush=True,
                )
                next_heartbeat = now + 30
            time.sleep(2)
        exit_code = process.returncode
    wall_seconds = round(time.monotonic() - started, 3)
    time.sleep(1)

    receipt_path = newest_added(receipts_before, dated_json_files(receipts_root))
    run_log_path = newest_added(runs_before, dated_jsonl_files(runs_root))
    cleanup = asyncio.run(cleanup_unity())
    result = {
        "run_id": run_id,
        "task": task,
        "repetition": repetition,
        "variant": variant,
        "model_key": model_key,
        "model": model,
        "exit_code": exit_code,
        "warm_seconds": warm_seconds,
        "wall_seconds": wall_seconds,
        "prompt_path": str(prompt_path),
        "stdout_path": str(stdout_path),
        "receipt_path": str(receipt_path) if receipt_path else None,
        "run_log_path": str(run_log_path) if run_log_path else None,
        "cleanup": cleanup,
        **receipt_metrics(receipt_path),
        **read_jsonl_metrics(run_log_path),
    }
    print(
        f"RUN_DONE {run_id} status={result['status']} build={result['build_stage_success']} "
        f"attempts={result['attempt_count']} wall={wall_seconds}s exit={exit_code}",
        flush=True,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--start-repetition", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--id-suffix", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "logs/model_benchmark/20260818")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["UNITY_PROJECT_DIR"] = str(args.project.resolve())

    summary_path = args.output / "summary.json"
    if summary_path.exists():
        results = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        results = []

    stop = args.start_repetition + args.repetitions
    selected_tasks = tuple(item.strip() for item in args.tasks.split(",") if item.strip())
    unknown_tasks = sorted(set(selected_tasks) - set(TASKS))
    if unknown_tasks:
        parser.error(f"unknown tasks: {', '.join(unknown_tasks)}")
    try:
        for repetition in range(args.start_repetition, stop):
            for task in selected_tasks:
                task_index = TASKS.index(task)
                order = ("A", "B") if (repetition + task_index) % 2 else ("B", "A")
                variants = ("X", "Y")
                for model_key, variant in zip(order, variants):
                    result = run_one(
                        project=args.project.resolve(), output_dir=args.output,
                        task=task, repetition=repetition, variant=variant,
                        model_key=model_key, id_suffix=args.id_suffix,
                    )
                    results.append(result)
                    summary_path.write_text(
                        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
    except KeyboardInterrupt:
        print("BENCHMARK_INTERRUPTED", flush=True)
        return 130
    print(f"BENCHMARK_COMPLETE runs={len(results)} summary={summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
