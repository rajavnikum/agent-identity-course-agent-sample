# Using your own conversational agent

1. Replace `llm_agent.py` or call your existing agent framework.
2. Map each supported tool/action to explicit scopes and resource context.
3. Keep the IBM Verify subject flow only for operations requiring human delegation.
4. Use an actor client associated with the registered agent.
5. Build authorization details from the actual selected operation and target, not arbitrary model prose.
6. Perform token exchange immediately before the protected operation when context is known.
7. Validate the delegated token at the resource boundary.
8. Remove tutorial diagnostics before deployment.
