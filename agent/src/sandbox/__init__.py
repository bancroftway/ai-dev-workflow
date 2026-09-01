"""Per-session sandbox provisioning (architecture plan Sections C/E).

A ``SandboxProvider`` starts a disposable container that clones a target repo/branch, then holds
it open for one-shot ``exec_in_sandbox`` calls -- both ``claude_chat_model.py`` and
``copilot_chat_model.py`` drive their CLI this way, a per-turn subprocess exec with no persistent
server or connection involved (see ``provider.py``'s own module docstring; Copilot's old
TCP/JSON-RPC session mechanism was fully retired by Task 3's CLI-exec rewrite). ``LocalDockerProvider``
targets a developer's own Docker Desktop/Engine; ``AzureContainerInstanceProvider``
targets Azure Container Instances (not Container Apps -- see its own module
docstring for why) for production. Both return the exact same ``SandboxSession``
shape so callers never know which is behind them.
"""

from . import registry
from .factory import get_sandbox_provider
from .provider import SandboxProvider, SandboxSession

__all__ = ["SandboxProvider", "SandboxSession", "get_sandbox_provider", "registry"]
