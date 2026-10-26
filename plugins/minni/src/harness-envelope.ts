// Prompt classifier (recall-to-use rev5, 5.2.1–5.2.2): re-exported from prompt-class.ts.
//
// Preserves the `harness-envelope.js -> harness-envelope-rules.js` load edge for
// `use-capture.js` and `use-detector.js` while S3a hook/harvest callers import
// `prompt-class.ts` directly so `MINNI_USE_LEDGER=off` never loads capture modules.
import "./harness-envelope-rules.js";

export {
  PROMPT_TEMPLATES_RELPATH,
  classifyPromptEnvelope,
  isCronScheduledPrompt,
  isPromptClassesEnabled,
  isScheduledTemplate,
  normalizeTemplateText,
  promptTemplatesPath,
  readPromptTemplates,
  readPromptTemplatesSync,
  recordOccurrence,
  stripHarnessBlocks,
  templateHash,
  type PromptEnvelope,
  type PromptEnvelopeKind,
  type PromptTemplateContext,
  type PromptTemplateEntry,
  type PromptTemplateInfo,
  type PromptTemplateOccurrence,
  type PromptTemplatesFile,
} from "./prompt-class.js";
