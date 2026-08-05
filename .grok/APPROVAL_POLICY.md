# Approval policy — executable agent workflows under .grok/

NEVER auto-approve changes under .grok/ (agent orchestration workflows,
sandbox profiles, capability configuration). These files define what the
reviewing agent itself executes; their review is always human.

Action: request human reviewers and do not approve, regardless of CI state,
Bugbot findings, or risk score.
