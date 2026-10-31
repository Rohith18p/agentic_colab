Make NVIDIA's world model, Cosmos3-Nano, better. Find where it gets the world wrong, work out why, fix it, and prove the fix on tests you have never seen, without making it worse at anything NVIDIA uses it for.

DELIVERABLES (due Sun 8:00 AM, see the timeline in Constraints)
1. The improved Nano: a fine-tuned checkpoint or LoRA adapter, or a recipe (prompts, settings, conditioning), with exact instructions to reproduce it.
2. What was wrong and why: a short list of replicated findings, each with numbers, uncertainty and evidence links.
3. Before/after on both scoreboards, measured on the dev sets.

HOW IT IS JUDGED (owned by the humans; agents can't change it)
- Sealed tests: real-world videos and prompts that no agent ever sees. The referee runs them after the freeze.
- Planted cases: clips with known answers. A measuring tool's numbers count only after it passes them.
- NVIDIA's own scores must not drop: visual quality (DOVER), motion fidelity, a plausibility judge, and prediction of real robot clips.

WHAT YOU DECIDE: what to improve, how to diagnose it, how to fix it, and when to pivot or stop.
