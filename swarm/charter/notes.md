STEERING (humans edit this section; agents follow the latest version)
- (empty)

STARTING CLUE (published, verified; not a plan)
- GAUGE (arXiv 2608.05948) measured Cosmos3-Nano image-to-video against real motion capture. The law's form is often right, but the constants are wrong:
  - Bouncing-ball free fall: 0.45 m/s² (real 9.81).
  - Pendulum period: 2.36 s (real 1.06).
  - Slope slide: wood 2.06 m/s² (real 2.58); plastic 0.58 (real 2.57); metal 0.36 (real 2.67).
- A single slow clock can't explain these: the implied time factor is about 4.7 from free fall but 2.2 from the pendulum. Nobody has explained why.
- The negative prompt alone moves the numbers (ball 0.45 → 0.33; wood slope 2.06 → 0.90). Inference settings matter.
- Every model GAUGE tested is wrong. The closest to 9.81 on the ball was Seedance 2 at 1.84; Nano's 0.45 was second.

BACKGROUND FACTS (with sources; use or ignore)
- From the Cosmos 3 tech report (arXiv 2606.02800):
  - Training data: 348M real clips in pre-training; mid-training added 75M curated clips, including simulated physics (SDG-PhyxSim; Isaac Lab collision clips).
  - FPS: the model is told the fps, both through time-proportional position encoding (base 24 fps) and in the prompt.
  - Evaluation: NVIDIA scored motion with the amount of motion and visual quality, and physics with VLM or human plausibility judges. Nothing measures constants.
- Fine-tuning precedents:
  - PISA (arXiv 2503.09595): about 5k simulated drop videos fine-tuned a 1B model, and it improved on 361 real drops (trajectory error 0.175 → 0.076). It degraded outside the trained heights, and it learned one typical fall rather than the real spread of outcomes.
  - Models trained on simulation tend to copy the nearest training case (arXiv 2411.02385).
- Looks and physics: visual realism is uncorrelated with physics accuracy (Physics-IQ, arXiv 2501.09038). The look of the first frame alone can shift measured physics a lot (Morpheus, arXiv 2504.02918).
- Hidden knowledge: correct physics is often decodable inside video models even when their outputs are wrong (arXiv 2606.05328, 2609.15980).
- Simulators aren't ground truth for everything. Per GAUGE, engines match reality on sliding and pendulum period, but are far off on impacts (bouncing, collisions) and cloth.

SCOREBOARD DETAILS
- Physics scoreboard: sealed real video, judged by the referee.
- NVIDIA scoreboard on dev prompts: DOVER (aesthetic and technical), motion fidelity (dynamic degree vs real at the same fps), a plausibility judge (same judge before and after), and real robot clip prediction (DROID or AgiBot held-out clips vs the real future). It must not drop beyond seed noise.
