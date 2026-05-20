# Vulnerability Inventory
**Date:** 2026-05-19
**Dimension:** Security

| ID | Component | Type | Severity | Description |
|----|-----------|------|----------|-------------|
| SEC-VULN-001 | `afm_writer.py` | Injection | High | Missing SEC-018 forgery guard; allows metadata spoofing. |
| SEC-VULN-002 | `afm_writer.py` | Injection | High | Unescaped YAML fields in frontmatter; allows metadata override. |
| SEC-VULN-003 | `episodic.py` | Leakage | Medium | Lack of redaction in episodic events; secrets leak to AFM prompts. |
| SEC-VULN-004 | `afm_provider.py` | SSRF | Low | Unvalidated `urlopen` in bridge client. |
| SEC-VULN-005 | `indexer.py` | Logic | Low | `re.match` on frontmatter skips blocks not at file start (potential for hidden metadata). |
