# Application runtime map

This supplement maps the sample code to the runtime security flow. The main README remains the canonical runbook.

## Request path

`app.py` receives the user interaction and coordinates intent selection, subject resolution, authorization-details construction, actor-token acquisition, token exchange, and the protected resource call.

`verify_oauth.py` owns OAuth protocol calls. `llm_agent.py` decides intent only; it is not an authorization engine. `rar_builder.py` creates operation context. `course_api.py` is the protected direct-tool/API boundary.

## Replacing the sample application

For your own agent, keep the same separation of concerns: authenticate the subject when human authority is required; authenticate the agent as an actor; build authorization context from the actual operation; request a delegated token; and enforce token/resource conditions at the protected API or MCP server. Do not authorize an operation solely because an LLM selected a tool.
