"""MCP control ownership status and explicit post-Escape resume."""

from mcp.types import ToolAnnotations

from windows_mcp.desktop.control import get_controller
from windows_mcp.desktop.control_context import current_owner


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

    @mcp.tool(
        name="ControlBegin",
        description="Begin an exclusive desktop control session and remember the original foreground window and placement. Keeps the blue frame and input capture between tools, including screenshots. End, physical Escape, timeout or native shutdown releases input and attempts to return to that window. Physical Escape cancels the entire session for all clients. Owner UI calls renew the abandonment timeout (30-900 seconds, default 300); use ControlKeepAlive during longer analysis and ControlEnd when finished. Never resume USER_CANCELLED without a new human instruction.",
        annotations=ToolAnnotations(title="ControlBegin", readOnlyHint=False,
            destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    def control_begin_tool(timeout_seconds: int = 300) -> dict:
        return get_controller().begin_session(current_owner.get(), timeout_seconds)

    @mcp.tool(
        name="ControlKeepAlive",
        description="Renew your existing desktop control session without mouse or keyboard actions. Does not start a session or clear Escape cancellation. Call before the abandonment timeout while analyzing; ControlStatus alone does not renew it.",
        annotations=ToolAnnotations(title="ControlKeepAlive", readOnlyHint=False,
            destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    def control_keepalive_tool() -> dict:
        return get_controller().keep_session(current_owner.get())

    @mcp.tool(
        name="ControlEnd",
        description="End your desktop control session, release the blue frame and physical input, and return to the original foreground window and placement when it still exists and Windows permits activation. Safe after Escape; does not clear cancellation or end another client's session. Always call when finished or before handing work to another chat.",
        annotations=ToolAnnotations(title="ControlEnd", readOnlyHint=False,
            destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    def control_end_tool() -> dict:
        return get_controller().end_session(current_owner.get())
