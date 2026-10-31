TIMELINE (US Eastern; hacking ends Sun Nov 1, 1:00 PM)
- Sat, as soon as compute and the referee are ready: swarm starts. Build and validate measuring tools first; nothing counts until a tool passes the planted cases.
- Sun 2:00 AM: last start time for any fine-tune run. After that, only evaluation and replication.
- Sun 6:00 AM: consolidation. No new lines of work. Replicate, finish evaluations, and draft the final report in the task the humans create for it.
- Sun 8:00 AM: freeze. Deliverables final. The referee runs the sealed tests; agents may only answer questions.

EVIDENCE RULES
- A finding is a number with an uncertainty, from a method that passed the planted cases. Use the format in the playbook.
- Only findings replicated by another agent (independent code or seeds) go in the final report.
- Every claimed improvement needs a control that would catch a fake win.
- Anything found on simulated or computer-rendered first frames must be re-checked on real photos before it is called a property of Cosmos.

DATA RULES
- Never look for, read, or train on sealed data. Sealed sets: see SEALED below. If you are unsure whether something is sealed, ask_human.
- Never probe the referee to recover planted answers. Submissions are rate-limited and logged on the board.
- Record exactly which data every fine-tune used.

SEALED (filled in by the referee before the swarm starts)
<FILL: list of sealed scenes/videos/prompts, e.g. "GAUGE trials X-Y", "PISA real test subset", "Principia scenes ...">

SHARED RESOURCES
- Fine-tunes and other big GPU jobs: post a proposal in #decisions first (data, expected effect, control, GPU-hours, evaluation). Go ahead if no human objects within 15 minutes.
- Stay inside your per-shift budget and the compute limits in Compute.
- Never put API keys, tokens or passwords on the board.
