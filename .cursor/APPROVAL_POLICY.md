# Approval policy — approval/routing policies under .cursor/

NEVER auto-approve changes under .cursor/ (approval policies, routing files,
Bugbot rules). These files define the repository's review and approval trust;
their review is always human.

Action: request human reviewers and do not approve, regardless of CI state,
Bugbot findings, or risk score.
