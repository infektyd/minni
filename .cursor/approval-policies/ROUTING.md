# Product routing for approval agents
# YAML list: product -> boundary -> policies

- product: ci-trust
  boundary: ".github/**"
  policies:
    - .github/APPROVAL_POLICY.md
- product: agent-workflows
  boundary: ".grok/**"
  policies:
    - .grok/APPROVAL_POLICY.md
- product: approval-policies
  boundary: ".cursor/**"
  policies:
    - .cursor/APPROVAL_POLICY.md
- product: minni-core
  boundary: "**"
  policies:
    - APPROVAL_POLICY.md
