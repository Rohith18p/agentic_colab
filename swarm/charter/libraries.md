MODEL
- Cosmos3-Nano, `nvidia/Cosmos3-Nano` on Hugging Face (16B mixture-of-transformers; not gated). Served with vLLM-Omni (`vllm serve nvidia/Cosmos3-Nano --omni`).
- Image-to-video: POST multipart to `<COSMOS_URL>/v1/videos/sync`. Running endpoints are announced in #compute; you can also serve your own if your GPU fits.
  - Image goes in the file field `input_reference`.
  - Form fields:
    - prompt: a JSON-structured caption, as a string
    - negative_prompt
    - size: e.g. "832x480"
    - num_frames
    - fps
    - num_inference_steps: default 35
    - guidance_scale: default 6.0
    - flow_shift: default 10.0
    - seed
    - extra_params: JSON
  - Prompt format and an upsampler: https://github.com/nvidia/cosmos-framework/blob/main/docs/prompt_upsampling.md. Example assets: `hf download nvidia/Cosmos3-Nano assets/`.
  - The model card (HF README) has the full request examples, including action-conditioned forward dynamics for supported robots (Franka, UR, WidowX, AgiBot, ...).
- Related checkpoints exist if useful: Cosmos3-Edge (small), Cosmos3-Nano-Policy-DROID, Cosmos3-Nano-Sim-Bimanual.

FINE-TUNING
- NVIDIA cosmos-framework (https://github.com/NVIDIA/cosmos-framework), recipe `vision_sft_nano`:
  - launch script `examples/launch_sft_vision_nano.sh`, config `examples/toml/sft_config/vision_sft_nano.toml`;
  - JSONL video+caption dataset (`train/video_dataset_file.jsonl`);
  - text-, image- and video-to-video.
- It trains only the generation tower (`moe_gen`, `time_embedder`, `vae2llm`, `llm2vae`). FSDP shards over however many GPUs run it (`NPROC_PER_NODE`). It was tested by NVIDIA only on 8xH100; fewer GPUs is untested.
- LoRA is not in the Nano recipe, but the switch exists; NVIDIA's Super recipe uses it. Copy into [model]:
  - lora_enabled = true
  - lora_rank = 16
  - lora_alpha = 32
  - lora_target_modules = "q_proj_moe_gen,k_proj_moe_gen,v_proj_moe_gen,o_proj_moe_gen"
  - with [model.ema] enabled = false, [optimizer] keys_to_select = ["lora_"] and lr = 5e-4, and [checkpoint] keys_to_skip_loading = ["net_ema.", "lora_"].
- Checkpoints are about 30 GB each. Mind the disk.

SIMULATION AND MEASUREMENT
- Simulators:
  - Isaac Sim / Isaac Lab: RTX rendering; needs RT cores, so it does not run on H100.
  - MuJoCo, Newton, Warp (differentiable), Genesis.
- Tracking: SAM 3 (what GAUGE and Principia use) or SAM 2.1. Fitting: scipy, PySR (symbolic regression).
- NVIDIA-scoreboard tools:
  - DOVER: video quality; NVIDIA uses it to filter Cosmos training data.
  - VBench "dynamic degree": NVIDIA's motion-fidelity metric compares it with real video at the same fps.
  - Cosmos-HUE plausibility questions (HF dataset `nvidia/Cosmos-HumanEval-v1`).
  - PAI-Bench (arXiv 2512.01989).
- Bulk analysis: a Gemini API key may be in your environment (cheap, high rate limit). Use it for bulk labeling, not for judging your own results.

DATA (dev splits only; see SEALED in Constraints)
- GAUGE: real motion capture with calibrated parameters. https://internrobotics.github.io/GAUGE
- PISA: 361 real drop videos plus a simulated set. https://github.com/vision-x-nyu/pisa-experiments
- Principia: real paired-object scenes, unit-free tests. https://PrincipiaBench.github.io
- Morpheus: 130 real videos of 9 physics experiments.
- Physics-IQ: real videos.
- Real robot video: DROID, Bridge, AgiBot (on HF).
