from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .requirements import WORKER_CODE_DIR


@dataclass(frozen=True)
class WorkerSpec:
    """How to run one worker agent in its own sbx sandbox.

    Args:
        name: Subagent name the supervisor delegates to.
        description: What the worker does; the supervisor's model reads it to pick a worker.
        graph_id: Graph in ``langgraph.json`` to delegate to.
        worker_dir: Host directory with ``langgraph.json`` (and optionally ``requirements.txt``).
            It becomes the sandbox workspace, shared read/write with the host. ``None`` mounts
            nothing from the host: the worker's code must then be in ``template`` at
            ``WORKER_CODE_DIR`` (``docker/worker.Dockerfile`` puts it there).
        secrets: sbx secret service name -> host environment variable holding its value, for
            example ``{"openai": "OPENAI_API_KEY"}``. Each value is stored with ``sbx secret set
            --sandbox`` for this sandbox only. The proxy injects it into matching requests; the
            VM only sees the variable set to ``proxy-managed``.
        env: Extra environment variables for the worker server, for example a model name.
            Never put secrets here: the VM can read them.
        allow_network: Hosts this sandbox may reach in addition to the global ``sbx policy``.
        deny_network: Hosts this sandbox may not reach. Deny wins over any allow, including
            ``**`` over every allow.
        kits: sbx kits added with ``--kit`` (references or local directories).
        agent: Built-in sbx agent or v3 workload the sandbox is created from.
        template: Sandbox template (``sbx create --template``) that already contains the worker
            venv; the dependency install is then skipped. Required without ``worker_dir``. See
            `subagents_docker_sbx.requirements`.
        memory: Sandbox memory, for example ``"2g"``; ``None`` leaves the sbx default.
        cpus: Whole CPUs; ``None`` leaves the sbx default.
        host_port: Port on host 127.0.0.1 for the worker; ``None`` picks a free one.
    """

    name: str
    description: str
    graph_id: str
    worker_dir: str | Path | None = None
    secrets: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    allow_network: Sequence[str] = ()
    deny_network: Sequence[str] = ()
    kits: Sequence[str | Path] = ()
    agent: str = "shell"
    template: str | None = None
    memory: str | None = None
    cpus: int | None = None
    host_port: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "WorkerSpec.name must not be empty"
            raise ValueError(msg)
        if self.worker_dir is None:
            # The code is in the template; its langgraph.json can't be checked from the host.
            if self.template is None:
                msg = "WorkerSpec without worker_dir needs a template that contains the worker"
                raise ValueError(msg)
        else:
            self._check_worker_dir(Path(self.worker_dir).resolve())
        clash = set(self.secrets.values()) & set(self.env)
        if clash:
            msg = f"env must not set secret variables: {sorted(clash)}"
            raise ValueError(msg)

    def _check_worker_dir(self, worker_dir: Path) -> None:
        object.__setattr__(self, "worker_dir", worker_dir)
        config_path = worker_dir / "langgraph.json"
        if not config_path.is_file():
            msg = f"{worker_dir} has no langgraph.json"
            raise ValueError(msg)
        graphs = json.loads(config_path.read_text()).get("graphs", {})
        if self.graph_id not in graphs:
            msg = f"graph {self.graph_id!r} is not in {config_path} (has: {sorted(graphs)})"
            raise ValueError(msg)

    @property
    def path(self) -> Path | None:
        """`worker_dir` as a resolved path, or ``None`` when nothing is mounted."""
        return None if self.worker_dir is None else Path(self.worker_dir)

    @property
    def workdir(self) -> str:
        """Directory inside the sandbox that `langgraph dev` serves the worker from."""
        return WORKER_CODE_DIR if self.worker_dir is None else str(self.worker_dir)
