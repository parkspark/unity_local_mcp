"""v1.7 E2E: '레벨 3개짜리 플랫포머 만들어줘' 전체 파이프라인 실행 (일회용 러너)."""
import asyncio
import sys

sys.path.insert(0, ".")

from agent import Agent
from mcp_client import UnityTools


async def main():
    async with UnityTools() as ut:
        agent = Agent(
            ut,
            on_text=lambda t: print(t, end="", flush=True),
            on_tool=lambda n, a, r: print(
                f"\n→ {n} {str(a)[:180]}\n← {r[:250]}", flush=True
            ),
            on_warn=lambda m: print(f"\n[warn] {m}", flush=True),
            on_milestone=lambda i, t, title: print(
                f"\n{'=' * 20} 마일스톤 {i + 1}/{t}: {title} {'=' * 20}", flush=True
            ),
        )
        await agent.run_turn("레벨 3개짜리 플랫포머 만들어줘")


if __name__ == "__main__":
    asyncio.run(main())
