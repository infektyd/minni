# Approval policy — CI trust surface

NEVER auto-approve changes under .github/ (workflows, gate scripts, leak
gates, actions configuration). These files define the repository's merge
trust; their review is always human.

Action: request human reviewers and do not approve, regardless of CI state,
Bugbot findings, or risk score.
