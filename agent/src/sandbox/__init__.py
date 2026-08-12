"""Per-session sandbox provisioning (architecture plan Sections C/E).

A ``SandboxProvider`` starts a disposable container that clones a target
repo/branch and runs the Copilot CLI runtime in TCP server mode, so
``copilot_chat_model.py`` can reach it via ``RuntimeConnection.for_uri``
instead of spawning Copilot as a local child process. ``LocalDockerProvider``
targets a developer's own Docker Desktop/Engine; ``AzureContainerInstanceProvider``
targets Azure Container Instances (not Container Apps -- see its own module
docstring for why) for production. Both return the exact same ``SandboxSession``
shape so callers never know which is behind them.
"""

from . import registry
from .factory import get_sandbox_provider
from .provider import SandboxProvider, SandboxSession

__all__ = ["SandboxProvider", "SandboxSession", "get_sandbox_provider", "registry"]
