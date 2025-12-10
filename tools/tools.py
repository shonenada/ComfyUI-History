#!/usr/bin/env python3
"""
Utility entrypoint for ComfyUI-History tools.

Commands:
  regen: regenerate image from PNG with embedded workflow
  info:  show basic info (prompt, steps, checkpoint) from PNG metadata
"""

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image

from tools.common import (
    cancel_prompt,
    convert_workflow_to_prompt,
    download_image,
    load_workflow_from_png,
    queue_prompt,
    register_signal_handlers,
    replace_embed_with_preview,
    set_clip_texts,
    set_seed,
    wait_for_result,
)


def command_regen(args):
    state = {"prompt_id": None}
    register_signal_handlers(args.host, state)

    workflow = load_workflow_from_png(args.png)
    workflow = set_clip_texts(workflow, args.prompt)
    try:
        seed_val = int(args.seed)
    except ValueError:
        seed_val = args.seed
    workflow = set_seed(workflow, seed_val)
    workflow = replace_embed_with_preview(workflow, enable=args.save_output)

    if args.debug:
        print("Submitting workflow:")
        print(json.dumps(workflow, indent=2))

    state["prompt_id"] = queue_prompt(args.host, workflow)
    result = wait_for_result(args.host, state["prompt_id"])
    out_path = args.out or Path(f"image_{int(time.time())}.png")
    out_path = download_image(args.host, result, out_path)

    if args.show:
        img = Image.open(out_path)
        img.show()
    else:
        print(f"Saved: {out_path.resolve()}")
    print(f"Prompt ID: {state['prompt_id']}")


def _info_from_prompt(prompt_map: dict):
    positive = None
    negative = None
    steps = None
    checkpoint = None
    for node in prompt_map.values():
        if not isinstance(node, dict):
            continue
        ctype = node.get("class_type")
        inputs = node.get("inputs") or {}
        if ctype in {"CLIPTextEncode", "CLIPTextEncodeSDXL"}:
            title = node.get("_meta", {}).get("title") or node.get("title", "")
            text_val = inputs.get("text")
            # heuristic: negative prompt node often has "Negative" in title
            if title and "Negative" in title and negative is None:
                negative = text_val
            elif positive is None:
                positive = text_val
            elif negative is None:
                negative = text_val
        if ctype == "KSampler" and steps is None:
            steps = inputs.get("steps")
        if ctype in {"CheckpointLoaderSimple", "CheckpointLoader", "CheckpointLoaderWithModel"} and checkpoint is None:
            checkpoint = inputs.get("ckpt_name")
        if ctype in {"UnetLoaderGGUF"} and checkpoint is None:
            checkpoint = inputs.get("unet_name")
    return positive, negative, steps, checkpoint


def command_info(args):
    data = load_workflow_from_png(args.png)
    if "nodes" in data:
        prompt_map = convert_workflow_to_prompt(data)
    else:
        prompt_map = data
    pos, neg, steps, ckpt = _info_from_prompt(prompt_map)
    print(f"Positive Prompt: {pos}")
    print(f"Negative Prompt: {neg}")
    print(f"Steps: {steps}")
    print(f"Checkpoint: {ckpt}")


def main():
    parser = argparse.ArgumentParser(description="ComfyUI-History tools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    regen = sub.add_parser("regen", help="Regenerate from PNG workflow")
    regen.add_argument("--png", required=True, type=Path, help="Path to PNG with workflow metadata.")
    regen.add_argument("--prompt", required=True, help="CLIP text prompt to inject.")
    regen.add_argument("--out", type=Path, default=None, help="Output PNG path (default image_<timestamp>.png).")
    regen.add_argument("--host", default="http://127.0.0.1:8188", help="ComfyUI API host.")
    regen.add_argument("--show", action="store_true", help="Display the resulting image after generation.")
    regen.add_argument("--debug", action="store_true", help="Print request payload before sending.")
    regen.add_argument("--seed", default=0, help="Seed for KSampler (0=random, fixed=keep original, number=override).")
    regen.add_argument("--save-output", action="store_true", help="Save via embedder (default false uses preview only).")
    regen.set_defaults(func=command_regen)

    info = sub.add_parser("info", help="Show prompt, steps, checkpoint from PNG metadata")
    info.add_argument("--png", required=True, type=Path, help="Path to PNG with workflow metadata.")
    info.set_defaults(func=command_info)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
