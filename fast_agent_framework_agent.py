import asyncio
import base64
import os

from dotenv import load_dotenv

## load .env BEFORE anything reads os.environ (fast-agent does not do this for us)
load_dotenv()

## fast-agent already ships OpenTelemetry instrumentation for the OpenAI/Anthropic
## SDKs and MCP. We just point its OTLP exporter at Langfuse and add the auth header.
## Langfuse authenticates OTLP with HTTP Basic: base64("<public_key>:<secret_key>")
auth = base64.b64encode(
    f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()
).decode()
os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {auth}"

from opentelemetry import trace

from fast_agent import FastAgent
from tools import fetch_youtube_transcript, get_arxiv_paper


def trace_turns(agent, root_span, trace_name="agent-session"):
    """Record trace-level input/output in Langfuse.

    fast-agent leaves its spans bare, so Langfuse shows no input/output on the
    trace. Langfuse only promotes `langfuse.trace.*` attributes from the ROOT
    span, so we tag that one - setting them on the per-turn span is ignored.
    Input is the first user message, output the most recent reply.
    """
    inner = agent.generate_impl
    seen_first = False

    async def wrapped(messages, request_params=None, tools=None):
        nonlocal seen_first
        root_span.set_attribute("langfuse.trace.name", trace_name)
        if not seen_first:
            root_span.set_attribute("langfuse.trace.input", messages[-1].last_text())
            seen_first = True
        response = await inner(messages, request_params, tools)
        root_span.set_attribute("langfuse.trace.output", response.last_text())
        return response

    agent.generate_impl = wrapped


# Create the application
fast = FastAgent("Agent Example")

@fast.agent(
  servers=["intercept","memory"],
  function_tools=[
    fetch_youtube_transcript,
    get_arxiv_paper,
  ],
  instruction="You have access to Intercept, a tool that fetches web content (YouTube, arXiv, PDFs, Wikipedia, GitHub) as clean markdown. Use it to fetch content when needed.",
)
async def main():
  async with fast.run() as agent:
    ## inside run() the current span is the trace root that Langfuse reads
    trace_turns(agent.default, trace.get_current_span())
    await agent.interactive()

if __name__ == "__main__":
    asyncio.run(main())
