# Security Audit Findings: Sovereign Memory (Agent A Findings)
**Dimension:** Security
**Model:** Gemini 3.5 Flash (High)
**Date:** 2026-05-19

| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| SEC-A01 | P1 | `afm_writer.py:133` | **Missing SEC-018 Forgery Guard**. Lacks checks for forged `---` lines in model output. | Uses `path.write_text(body)` without validation. | Port guard from `writeback.py`. |
| SEC-A02 | P1 | `afm_writer.py:78` | **YAML Injection via Unescaped Fields**. `title` and `tags` can inject new YAML keys. | F-string construction with unescaped variables. | Use proper YAML dumper. |
| SEC-A03 | P2 | `episodic.py:82` | **Secret Leakage in Episodic Events**. Raw secrets are stored and sent to AFM providers. | `add_event` lacks redaction; distillation includes raw text. | Implement redaction in core logic. |
| SEC-A04 | P3 | `afm_provider.py:211` | **Insecure urlopen (SSRF Vector)**. `urlopen` used without strict host allowlist. | Unvalidated bridge client requests. | Restrict to `localhost`. |
