"""Send ONE tiny structured request and report the outcome. No retries, no repair.

Usage (from backend/):  uv run python -m grocery_planner.llm.probe [--model gemini-3.5-flash-lite]
"""

import argparse
import sys
import time

from pydantic import BaseModel

from grocery_planner.llm.gemini import GeminiClient
from grocery_planner.llm.types import LLMError


class Pong(BaseModel):
    reply: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="model id (default: GEMINI_MODEL or the built-in default)")
    args = parser.parse_args(argv)
    client = GeminiClient(model=args.model, single_shot=True)
    start = time.perf_counter()
    try:
        result = client.generate("Reply with one word.", "Say hi.", Pong)
    except LLMError as e:
        ms = round((time.perf_counter() - start) * 1000)
        cause = e.__cause__
        detail = f" ({getattr(cause, 'code', '')} {getattr(cause, 'status', '')})" if cause else ""
        print(f"model={client.model} FAILED {type(e).__name__}{detail} after {ms}ms: {e}")
        return 1
    u = result.usage
    print(
        f"model={result.model} OK latency={result.latency_ms}ms requests={u.requests} "
        f"input_tokens={u.input_tokens} output_tokens={u.output_tokens} "
        f"thinking_tokens={u.thinking_tokens}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
