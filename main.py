"""로컬 LLM(Ollama) Unity 에이전트 CLI.

사용법:
    uv run main.py            # 채팅 시작
    uv run main.py --vision   # /look 스크린샷 분석 활성화 (qwen2.5vl 필요)
    uv run main.py --project "D:\\UnityProjects\\MyGame"
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import ollama
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape

import config
from agent import Agent
from bridge_install import ensure_bridge_dependencies, ensure_bridge_installed, ensure_new_input_is_enabled
from mcp_client import UnityTools
from project_settings import select_project

console = Console()

HELP = """\
명령: /reset 대화 초기화 · /tools 도구 목록 · /model <이름> 모델 변경
      /last 마지막 도구 결과(절단 전) · /log 마지막 실행 로그 경로
      /receipt 마지막 호스트 검증 영수증
      /verify <요청> 쓰기 도구를 차단한 검증 전용 실행
      /look [질문] 스크린샷 분석(--vision) · /quit 종료"""


def _find_png(result_text: str) -> str | None:
    """도구 결과 JSON에서 .png 경로를 재귀적으로 찾는다."""
    try:
        data = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return None

    def walk(node):
        if isinstance(node, str) and node.lower().endswith(".png"):
            return node
        if isinstance(node, dict):
            for v in node.values():
                if (found := walk(v)):
                    return found
        if isinstance(node, list):
            for v in node:
                if (found := walk(v)):
                    return found
        return None

    return walk(data)


class Cli:
    def __init__(self, vision: bool):
        self.vision = vision
        self.last_screenshot: str | None = None

    def on_text(self, chunk: str):
        print(chunk, end="", flush=True)

    def on_tool(self, name: str, args: dict, result: str):
        arg_str = json.dumps(args, ensure_ascii=False)
        console.print(f"\n[cyan]→ {name}[/cyan] [dim]{escape(arg_str)}[/dim]")
        preview = result[:200] + ("…" if len(result) > 200 else "")
        console.print(f"[dim]← {escape(preview)}[/dim]")

        if name == "unity_screenshot" and (png := _find_png(result)):
            self.last_screenshot = png
            console.print(f"[bold green]스크린샷: {png}[/bold green]")
            if config.AUTO_OPEN_SCREENSHOT and os.path.exists(png):
                os.startfile(png)

    def on_warn(self, msg: str):
        console.print(f"\n[yellow]경고: {msg}[/yellow]")

    def on_milestone(self, idx: int, total: int, title: str):
        console.rule(f"[bold magenta]마일스톤 {idx + 1}/{total}: {title}[/bold magenta]")

    async def look(self, agent: Agent, question: str):
        if not self.vision:
            console.print("[yellow]--vision 플래그로 시작해야 /look을 쓸 수 있습니다.[/yellow]")
            return
        if not self.last_screenshot or not os.path.exists(self.last_screenshot):
            console.print("[yellow]분석할 스크린샷이 없습니다. 먼저 스크린샷을 찍으세요.[/yellow]")
            return
        console.print(f"[dim]{config.VISION_MODEL}로 분석 중…[/dim]")
        resp = await ollama.AsyncClient().chat(
            model=config.VISION_MODEL,
            messages=[{
                "role": "user",
                "content": question or "이 Unity 화면에 무엇이 보이는지 설명해줘.",
                "images": [self.last_screenshot],
            }],
        )
        desc = resp.message.content or "(응답 없음)"
        console.print(desc)
        # 코더 모델이 활용할 수 있게 히스토리에 주입
        agent.history.append(
            {"role": "user", "content": f"[화면 분석 결과] {desc}"}
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="로컬 LLM으로 Unity Editor를 제어합니다.")
    parser.add_argument("--vision", action="store_true", help="스크린샷 비전 분석 활성화")
    parser.add_argument(
        "--project",
        metavar="PATH",
        help="연결할 Unity 프로젝트 루트(Assets와 ProjectSettings가 있는 폴더)",
    )
    parser.add_argument(
        "--prompt-file",
        metavar="PATH",
        help="UTF-8 프롬프트 파일 전체를 한 번 실행하고 종료",
    )
    parser.add_argument(
        "--repair-existing",
        action="store_true",
        help="초기 builder를 건너뛰고 기존 산출물의 실패 항목만 검증·수정",
    )
    return parser.parse_args(argv)


def _browse_for_project() -> str | None:
    """Open a native folder picker only when a user explicitly requests it."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="Select Unity project (Assets and ProjectSettings)")
        root.destroy()
        return selected or None
    except Exception:
        return None


def _select_interactively(default_project: str) -> str | None:
    """Allow normal CLI startup to choose a project instead of silently reusing one."""
    try:
        suggestion = select_project(None, default_project).path
    except ValueError:
        suggestion = default_project
    console.print(f"Unity project (Enter = recent): [cyan]{suggestion}[/cyan]")
    console.print("Paste a Unity project folder, or enter [cyan]b[/cyan] to browse.")
    try:
        answer = input("Unity project> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer.lower() in {"b", "browse"}:
        answer = _browse_for_project() or ""
        if not answer:
            return None
    return answer or suggestion


async def main(args: argparse.Namespace | None = None):
    args = args or _parse_args()
    project_arg = args.project
    # Explicit command-line and environment configuration stay non-interactive
    # for CI and scripts. A person launching the normal REPL gets a choice.
    if not project_arg and not os.environ.get("UNITY_PROJECT_DIR") and sys.stdin.isatty():
        project_arg = _select_interactively(config.UNITY_PROJECT_DIR)
        if project_arg is None:
            console.print("[yellow]Project selection cancelled.[/yellow]")
            return 2
    try:
        selection = select_project(project_arg, config.UNITY_PROJECT_DIR)
    except ValueError as exc:
        console.print(f"[bold red]프로젝트 설정 오류:[/bold red] {escape(str(exc))}")
        return 2

    config.UNITY_PROJECT_DIR = selection.path
    packages = ensure_bridge_dependencies(selection.path)
    if packages.status == "packages_added":
        console.print(f"[green]Installed Unity packages: {', '.join(packages.added)}[/green]")
    elif packages.status not in {"already_ready"}:
        console.print(
            f"[bold red]Package setup failed:[/bold red] "
            f"{escape(packages.message or '')}"
        )
        return 2
    if ensure_new_input_is_enabled(selection.path):
        console.print("[green]Enabled Unity Input System compatibility (Both).[/green]")

    bridge = ensure_bridge_installed(
        selection.path,
        Path(config.UNITY_MCP_DIR) / "UnityBridge" / "UnityMcpBridge.cs",
    )
    if bridge.status == "installed":
        console.print(f"[green]Installed Unity MCP bridge: {bridge.path}[/green]")
        console.print(
            "[yellow]Return to Unity and wait for compilation. "
            "The Console must show '[McpBridge] Listening' before connecting.[/yellow]"
        )
    elif bridge.status in {"source_missing", "install_failed"}:
        console.print(
            f"[bold red]Bridge installation failed:[/bold red] "
            f"{escape(bridge.message or '')}"
        )
        return 2
    console.print(f"[dim]Unity 프로젝트 ({selection.source}): {selection.path}[/dim]")
    if selection.warning:
        console.print(f"[yellow]경고: {escape(selection.warning)}[/yellow]")

    cli = Cli(args.vision)

    async with UnityTools() as ut:
        agent = Agent(ut, cli.on_text, cli.on_tool, cli.on_warn, on_milestone=cli.on_milestone)
        console.print(
            f"[bold]연결됨:[/bold] {len(ut.ollama_tools)} tools · {agent.model} · ctx {config.NUM_CTX}"
        )

        ping = await ut.call("unity_ping", {})
        try:
            info = json.loads(ping)
            if info.get("status") == "ok":
                r = info["result"]
                console.print(
                    f"[green]Unity {r.get('unityVersion')} · {r.get('productName')}[/green]"
                )
            else:
                console.print(f"[yellow]{escape(str(info.get('error')))}[/yellow]")
        except (json.JSONDecodeError, KeyError):
            console.print(f"[yellow]{escape(ping)}[/yellow]")

        console.print(HELP)
        if ut.last_audit_log_path:
            console.print(f"[dim]MCP 감사 로그: {ut.last_audit_log_path}[/dim]")
        elif ut.audit_log_error:
            console.print(f"[yellow]MCP 감사 로그를 시작하지 못했습니다: {escape(ut.audit_log_error)}[/yellow]")

        if args.prompt_file:
            try:
                prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                console.print(f"[bold red]프롬프트 파일 오류:[/bold red] {escape(str(exc))}")
                return 2
            if not prompt:
                console.print("[bold red]프롬프트 파일이 비어 있습니다.[/bold red]")
                return 2
            success = await agent.run_turn(
                prompt, repair_existing=bool(getattr(args, "repair_existing", False))
            )
            print()
            if agent.last_run_log_paths:
                text_path, jsonl_path = agent.last_run_log_paths
                console.print(f"[dim]실행 로그: {text_path}[/dim]")
                console.print(f"[dim]JSONL: {jsonl_path}[/dim]")
            if agent.last_verification_receipt_path:
                console.print(
                    f"[dim]검증 영수증: {agent.last_verification_receipt_path}[/dim]"
                )
            return 0 if success else 1

        try:
            session: PromptSession | None = PromptSession()
        except Exception:
            session = None  # 파이프 입력 등 콘솔이 아닌 환경 → 기본 input 폴백

        async def read_input() -> str:
            if session is not None:
                with patch_stdout():
                    return await session.prompt_async("\nyou> ")
            print("\nyou> ", end="", flush=True)
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                raise EOFError
            return line

        while True:
            try:
                user = (await read_input()).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user:
                continue

            if user.startswith("/"):
                cmd, _, rest = user.partition(" ")
                if cmd in ("/quit", "/exit", "/q"):
                    break
                elif cmd == "/reset":
                    agent.reset()
                    console.print("[dim]대화를 초기화했습니다.[/dim]")
                elif cmd == "/tools":
                    for t in ut.ollama_tools:
                        fn = t["function"]
                        desc = (fn["description"] or "").split("\n")[0].split(". ")[0]
                        console.print(f"  [cyan]{fn['name']}[/cyan] [dim]{escape(desc)}[/dim]")
                elif cmd == "/model":
                    if rest:
                        agent.model = rest.strip()
                        console.print(f"[dim]모델: {agent.model}[/dim]")
                    else:
                        console.print(f"[dim]현재 모델: {agent.model}[/dim]")
                elif cmd == "/last":
                    if ut.last_raw_result:
                        console.print(escape(ut.last_raw_result))
                    else:
                        console.print("[dim](없음)[/dim]")
                elif cmd == "/log":
                    if agent.last_run_log_paths:
                        text_path, jsonl_path = agent.last_run_log_paths
                        console.print(f"[dim]텍스트 로그: {text_path}[/dim]")
                        console.print(f"[dim]JSONL 로그: {jsonl_path}[/dim]")
                    else:
                        console.print("[dim](아직 저장된 실행 로그가 없습니다)[/dim]")
                    if ut.last_audit_log_path:
                        console.print(f"[dim]MCP 감사 로그: {ut.last_audit_log_path}[/dim]")
                    if agent.last_verification_receipt_path:
                        console.print(
                            f"[dim]검증 영수증: {agent.last_verification_receipt_path}[/dim]"
                        )
                elif cmd == "/receipt":
                    if agent.last_verification_receipt_path:
                        console.print(agent.last_verification_receipt_path)
                    else:
                        console.print("[dim](아직 저장된 검증 영수증이 없습니다)[/dim]")
                elif cmd == "/verify":
                    if not rest.strip():
                        console.print("[yellow]사용법: /verify <검증할 내용>[/yellow]")
                        continue
                    try:
                        await agent.run_turn(rest.strip(), tool_mode="verify")
                        print()
                    except KeyboardInterrupt:
                        console.print("\n[yellow]검증을 중단했습니다.[/yellow]")
                    except Exception as e:
                        console.print(f"\n[red]{type(e).__name__}: {escape(str(e))}[/red]")
                    finally:
                        if agent.last_run_log_paths:
                            text_path, jsonl_path = agent.last_run_log_paths
                            console.print(f"[dim]실행 로그: {text_path}[/dim]")
                            console.print(f"[dim]JSONL: {jsonl_path}[/dim]")
                elif cmd == "/look":
                    await cli.look(agent, rest.strip())
                else:
                    console.print(HELP)
                continue

            try:
                await agent.run_turn(user)
                print()  # 스트리밍 마무리 개행
            except KeyboardInterrupt:
                console.print("\n[yellow]턴을 중단했습니다.[/yellow]")
            except ollama.ResponseError as e:
                console.print(f"\n[red]Ollama 오류: {escape(str(e.error))}[/red]")
                if "not found" in str(e.error).lower():
                    console.print(f"[yellow]모델을 받으세요: ollama pull {agent.model}[/yellow]")
            except Exception as e:
                console.print(f"\n[red]{type(e).__name__}: {escape(str(e))}[/red]")
            finally:
                if agent.last_run_log_paths:
                    text_path, jsonl_path = agent.last_run_log_paths
                    console.print(f"[dim]실행 로그: {text_path}[/dim]")
                    console.print(f"[dim]JSONL: {jsonl_path}[/dim]")

    console.print("[dim]종료합니다.[/dim]")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
