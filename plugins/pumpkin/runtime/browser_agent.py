"""Compatibility exports for the browser controller.

New code belongs in ``browser``. This module remains so existing runtime and
test imports do not need to cross the migration boundary at once.
"""

from browser.controller import (
    BrowserAgentRunner,
)
from browser.models import (
    BrowserAgentError,
    BrowserArgumentError,
    BrowserArtifact,
    BrowserArtifactMode,
    BrowserEvidenceRequirement,
    BrowserExecutionState,
    BrowserExpectedOutput,
    BrowserInteractionMode,
    BrowserRequiredCapability,
    BrowserRunResult,
    BrowserTaskContract,
    BrowserTaskStatus,
)

__all__ = [
    "BrowserAgentError",
    "BrowserAgentRunner",
    "BrowserArgumentError",
    "BrowserArtifact",
    "BrowserArtifactMode",
    "BrowserEvidenceRequirement",
    "BrowserExecutionState",
    "BrowserExpectedOutput",
    "BrowserInteractionMode",
    "BrowserRequiredCapability",
    "BrowserRunResult",
    "BrowserTaskContract",
    "BrowserTaskStatus",
]
