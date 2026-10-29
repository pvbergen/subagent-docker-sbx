"""Worker agent served by `langgraph dev` from inside an sbx sandbox.

The whole agent loop runs in the sandbox: model calls, tools and its (in-state) files. The
OpenAI key is never present here; `OPENAI_API_KEY` holds the placeholder `proxy-managed` and
the sbx proxy injects the real key into requests to api.openai.com.
"""

import os

from deepagents import create_deep_agent

graph = create_deep_agent(
    model=os.environ.get("WORKER_MODEL", "openai:gpt-5.5"),
    system_prompt=(
        "You are a worker agent running inside an isolated sandbox. "
        "Complete the task you are given and answer concisely."
    ),
)
