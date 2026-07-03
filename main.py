"""로컬 LLM(Ollama) Unity 에이전트 CLI.

사용법:
    uv run main.py            # 채팅 시작
    uv run main.py --vision   # /look 스크린샷 분석 활성화 (qwen2.5vl 필요)
"""

import asyncio
import json
import os
import sys

import ollama
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markup import escape

import config
from agent import Agent
from mcp_client import UnityTools

console = Console()

HELP = """\
명령: /reset 대화 초기화 · /tools 도구 목록 · /model <이름> 모델 변경
      /last 마지막 도구 결과(절단 전) · /look [질문] 스크린샷 분석(--vision) · /quit 종료"""


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


async def main():
    vision = "--vision" in sys.argv
    cli = Cli(vision)

    async with UnityTools() as ut:
        agent = Agent(ut, cli.on_text, cli.on_tool, cli.on_warn)
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

    console.print("[dim]종료합니다.[/dim]")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
        sys.stdin.reconfigure(encoding="utf-8")
    asyncio.run(main())
