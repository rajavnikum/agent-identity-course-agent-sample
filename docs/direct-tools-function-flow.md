# Direct Tool Function Flow

This document maps user prompts to the exact Python functions used by the sample.

## Tool mapping

| Prompt example | Action selected by `decide_action()` | Protected function |
| --- | --- | --- |
| `What courses are available?` | `list_available_courses` | `call_course_api()` |
| `Show my enrolled courses` | `list_enrolled_courses` | `call_course_api()` |
| `Enroll me into advanced security training` | `enroll_course` | `call_course_api()` |

## Source files

- `app.py` — `/chat` orchestration and protected tool invocation.
- `llm_agent.py` — `decide_action()` and the allow-listed agent actions.
- `rar_builder.py` — `build_agent_authorization_details()`.
- `verify_oauth.py` — `get_actor_token()` and `token_exchange()`.
- `course_api.py` — `call_course_api()` and resource-side authorization checks.

## Exact runtime chain

```text
POST /chat
  -> app.chat()
  -> llm_agent.decide_action()
  -> app.resolve_target_subject()
  -> rar_builder.build_agent_authorization_details()
  -> verify_oauth.get_actor_token()
  -> verify_oauth.token_exchange()
  -> course_api.call_course_api()
       -> _validate_scope()
       -> _validate_audience()
       -> _validate_actor()
       -> _validate_authorization_details()
       -> execute selected operation
  -> app.build_answer()
```

## Important design point

`decide_action()` performs intent classification. It is not an authorization function.

`call_course_api()` is the protected resource boundary. The selected business operation is executed only after the delegated token has passed the sample's scope, audience, actor, authorization-details, and self-service checks.
