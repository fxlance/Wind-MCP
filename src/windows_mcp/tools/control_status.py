"""MCP control ownership status and explicit post-Escape resume."""

from mcp.types import ToolAnnotations

from windows_mcp.desktop.control import get_controller


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="ControlStatus",
        description="Get control status. After Escape/USER_CANCELLED, resume=True may be used only after the user explicitly asks to continue.",
        annotations=ToolAnnotations(
            title="ControlStatus",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def control_status_tool(resume: bool = False) -> dict:
        controller = get_controller()
        return controller.resume_control() if resume else controller.status()
