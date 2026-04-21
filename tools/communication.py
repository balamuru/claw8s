
import logging
from .registry import registry, ToolResult

log = logging.getLogger(__name__)

@registry.tool(
    name="send_status_update",
    description="Sends an intermediate status update to the user via Telegram. Use this to keep the human informed during long-running operations like rollout waits.",
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The status message to send (e.g. 'Remediation applied, waiting for rollout...')"}
        },
        "required": ["message"]
    }
)
async def send_status_update(message: str):
    # This tool will be 'hooked' in agent.py to call the bot
    return ToolResult(
        success=True,
        output=f"Status update sent: {message}"
    )
