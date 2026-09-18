from tools.web import fetch_page

TOOL_REGISTRY = {
    "web_fetch": fetch_page
}


def run_tool(name, args):
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        return {"error": "unknown tool"}
    args = args if isinstance(args, dict) else {}
    return tool(**args)
