# Roadmap

Last updated: 2026-09-29

This is a short, near-term snapshot of current priorities and what comes next — not
strategic direction. It has no fixed owner or update cadence yet; anyone can edit it
directly, and it should stay short rather than exhaustively maintained. Durable
strategic direction lives in the North Star document, not here. If an item below turns
out to represent a strategic shift rather than a near-term step, take it to North Star
review instead of just editing this file.

## Current near-term priorities

1. **Stateful foundation slice** (Lifecycle root: Asana 1218348601889574) — first
   priority. The inert durable-operations foundation spec is review-PASS/READY; it
   still needs a final live Human Review before dispatch, and no implementation has
   landed yet.
2. **Assurance outcome-fidelity prototype** (Asana 1218572709589612) — second
   priority, after the Stateful slice. Prototype design is terminal PASS/READY; it is
   read-only/offline only, with no production enforcement authority.
3. **Wakeful runtime-capability prototype** (Asana 1218483143301526) — third priority,
   after Assurance. Design is PASS/READY (Prototype A, then B conditional on A);
   production implementation stays deferred until prototype evidence lands.
4. **Handoff / current-workset V1** (Asana 1218940528735178) — design is terminal
   PASS; focused rereview (F1: nonterminal review/message/watch continuity) also
   cleared. Awaiting its first pilot at SW — Asana Agent once the bootstrap callable
   surface is available.

## Notes

- Ordering above (Stateful → Assurance → Wakeful) reflects Marco's explicit
  2026-09-27 direction on the Stateful root task and should be kept in sync with that
  task if it changes.
- Handoff/current-workset V1 is design-complete and pilot-ready independently of the
  Stateful/Assurance/Wakeful sequencing above; it is not blocked on that ordering.
