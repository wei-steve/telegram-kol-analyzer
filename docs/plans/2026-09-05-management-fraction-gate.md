# Illegal management fraction gate implementation plan

Goal: distinguish missing quantities (existing 50% default) from supplied invalid quantities (reject).

Architecture: a dedicated ValueError subtype carries management_fraction_invalid and bounded source/classification evidence. The parser returns a float only for a valid fraction, None only for absent/blank content, and raises for malformed, nonfinite or out-of-range content. Explicit percent strings divide by 100; bare values must already lie in (0, 1]. Both close and retained text extraction preserve signs and reject invalid matches; retained values must also produce an executable close fraction. Authoritative recognition validates before candidate/item projection and persists a dedicated mandatory Runtime Incident after its rejection transaction commits. Existing ambiguity semantics remain; no new database columns.

1. Add parser and authoritative-apply regression tests, run RED on current implementation.
2. Implement three-state parsing and shared text checks; wire reason and Incident without exchange calls or fallback execution. Preflight supplied instruction parameters and invalid target fields before normalization can discard them. Keep the existing target schema restriction (no target-specific fraction fields); do not broaden schema or claim per-target fraction isolation. Apply the same rejection/Incident behavior to V1 lifecycle recognition.
3. Run focused tests including missing-value/default behavior, malformed content, signed and out-of-range text, and incident/zero-instruction assertions; independent review under requesting-code-review.
4. Run complete pytest on final code, record counts, commit explicit files on codex/management-fraction-gate (base d1e3d8582501c54f1b9c105a0058c224e902c824). No merge, deployment, history replay, schema/data changes or live exchange writes.

Rollback: local unmerged candidate only. No production action. Stop-loss gate remains inherited and unchanged; take-profit magnitude and leverage remain separate work.
