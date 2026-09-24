from tools.browser import (
    browser_click,
    browser_goto,
    browser_login,
    browser_read,
    browser_submit,
    browser_type,
)
from tools.web import fetch_page

TOOL_REGISTRY = {
    "web_fetch": fetch_page,
    "browser_goto": browser_goto,
    "browser_read": browser_read,
    "browser_click": browser_click,
    "browser_type": browser_type,
    "browser_submit": browser_submit,
    "browser_login": browser_login,
}


def run_tool(name, args, bot_name=None, user_id=None):
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        return {"error": "unknown tool", "status": "error"}
    args = dict(args) if isinstance(args, dict) else {}
    if bot_name and "persona" not in args:
        args["persona"] = bot_name
    if user_id and "user_id" not in args:
        args["user_id"] = user_id
    try:
        return tool(**args)
    except TypeError as exc:
        return {"error": str(exc), "status": "error"}
    except Exception as exc:
        return {"error": str(exc), "status": "error"}
