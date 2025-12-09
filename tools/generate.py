#!/usr/bin/env python3
"""
Generate an image using a workflow embedded in a PNG and a provided CLIP text prompt.

Usage:
  python tools/generate_from_png.py --png /path/to/input.png --prompt "a cat" [--out out.png] [--show] [--host http://127.0.0.1:8188]

Requirements:
  - ComfyUI server running with the API enabled.
  - Python packages: requests, pillow
"""

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from PIL import Image


def load_workflow_from_png(png_path: Path) -> dict:
    with Image.open(png_path) as img:
        info = img.info or {}
    raw = info.get("prompt") or info.get("workflow")
    if not raw:
        raise RuntimeError("No workflow metadata found in PNG (expected 'workflow' or 'prompt').")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Workflow metadata is not valid JSON: {e}") from e
    # Prefer direct prompt mapping if present
    if isinstance(data, dict) and "prompt" in data and isinstance(data["prompt"], dict):
        return data["prompt"]
    # Some embeds store the workflow inside a wrapper
    if isinstance(data, dict) and "nodes" not in data and "workflow" in data:
        data = data["workflow"]
    if isinstance(data, dict) and "nodes" in data:
        return convert_workflow_to_prompt(data)
    if isinstance(data, dict):
        return data
    raise RuntimeError("Unsupported workflow format in PNG metadata.")


def set_clip_texts(workflow: dict, prompt_text: str) -> dict:
    # Update all CLIPTextEncode nodes to the given prompt text.
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        node_type = node.get("class_type") or node.get("type")
        if node_type not in {"CLIPTextEncode", "CLIPTextEncodeSDXL"}:
            continue
        inputs = node.get("inputs") or {}
        if isinstance(inputs, dict):
            inputs["text"] = prompt_text
            node["inputs"] = inputs
    return workflow


def set_seed(workflow: dict, seed):
    """Set seed on KSampler nodes. seed==0 => random, 'fixed' => keep, else override."""
    if seed == "fixed":
        return workflow
    import random

    seed_value = random.randint(0, 2**63 - 1) if seed == 0 else seed
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        node_type = node.get("class_type") or node.get("type")
        if node_type != "KSampler":
            continue
        inputs = node.get("inputs") or {}
        if isinstance(inputs, dict):
            inputs["seed"] = seed_value
            node["inputs"] = inputs
    return workflow


def replace_embed_with_preview(workflow: dict, enable: bool):
    if enable:
        return workflow
    # Replace PromptWorkflowEmbedder with PreviewImage to avoid saving to output
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") == "PromptWorkflowEmbedder":
            node["class_type"] = "PreviewImage"
    return workflow


def queue_prompt(host: str, workflow: dict) -> str:
    payload = {"prompt": workflow}
    errors = []
    for endpoint in ("/api/prompt", "/prompt"):
        url = urljoin(host if host.endswith("/") else host + "/", endpoint.lstrip("/"))
        try:
            resp = requests.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise RuntimeError("No prompt_id returned from server.")
            return prompt_id
        except Exception as e:
            errors.append((endpoint, str(e)))
    raise RuntimeError(f"Failed to queue prompt via endpoints {errors}")


def wait_for_result(host: str, prompt_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        errors = []
        for endpoint in (f"/api/history/{prompt_id}", f"/history/{prompt_id}"):
            url = urljoin(host if host.endswith("/") else host + "/", endpoint.lstrip("/"))
            try:
                resp = requests.get(url)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()
                if prompt_id in data:
                    return data[prompt_id]
            except Exception as e:
                errors.append((endpoint, str(e)))
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for prompt {prompt_id}")


def download_image(host: str, result: dict, out_path: Path):
    images = result.get("outputs", {}).values()
    first_img = None
    for output in images:
        if not isinstance(output, dict):
            continue
        imgs = output.get("images") or []
        if imgs:
            first_img = imgs[0]
            break
    if not first_img:
        raise RuntimeError("No image found in result.")
    subfolder = first_img.get("subfolder", "")
    name = first_img.get("filename")
    if not name:
        raise RuntimeError("Result image missing filename.")
    params = {"filename": name, "subfolder": subfolder, "type": first_img.get("type", "output")}
    errors = []
    for endpoint in ("/api/view", "/view"):
        url = urljoin(host if host.endswith("/") else host + "/", endpoint.lstrip("/"))
        try:
            resp = requests.get(url, params=params)
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
            return out_path
        except Exception as e:
            errors.append((endpoint, str(e)))
    raise RuntimeError(f"Failed to download image via endpoints {errors}")


def cancel_prompt(host: str, prompt_id: str):
    errors = []
    payload = {"prompt_id": prompt_id}
    # Preferred: interrupt with prompt_id
    for endpoint in ("/api/interrupt", "/interrupt"):
        url = urljoin(host if host.endswith("/") else host + "/", endpoint.lstrip("/"))
        try:
            print(f"[cancel] POST {url} (interrupt) for {prompt_id}")
            resp = requests.post(url, json=payload, timeout=5)
            print(resp.content)
            return True
        except Exception as e:
            errors.append((endpoint, str(e)))
    # Queue cancel endpoints
    for endpoint in ("/api/cancel", "/prompt_cancel", "/api/queue/cancel", "/queue/cancel"):
        url = urljoin(host if host.endswith("/") else host + "/", endpoint.lstrip("/"))
        try:
            print(f"[cancel] POST {url} for prompt {prompt_id}")
            resp = requests.post(url, json=payload, timeout=5)
            print(resp.content)
            return True
        except Exception as e:
            errors.append((endpoint, str(e)))
    print(f"Failed to cancel prompt: {errors}")
    return False


def convert_workflow_to_prompt(workflow: dict) -> dict:
    """
    Convert workflow JSON (as stored in PNG metadata) to a prompt mapping for the API.
    """
    prompt = {}
    nodes = workflow.get("nodes", [])
    links = workflow.get("links", [])
    link_lookup = {}
    for link in links:
        # link format: [id, start_node, start_slot, end_node, end_slot, type]
        if len(link) < 5:
            continue
        _, start, start_slot, end, end_slot = link[:5]
        link_lookup[(end, end_slot)] = (start, start_slot)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id"))
        class_type = node.get("class_type") or node.get("type")
        if not class_type:
            raise RuntimeError(f"Node {node_id} missing class/type.")
        inputs_map = {}
        widget_values = node.get("widgets_values") or []
        w_idx = 0
        for idx, input_def in enumerate(node.get("inputs", []) or []):
            name = input_def.get("name") or f"input_{idx}"
            link_key = (node.get("id"), idx)
            if link_key in link_lookup:
                start, start_slot = link_lookup[link_key]
                inputs_map[name] = [str(start), start_slot]
            else:
                if w_idx < len(widget_values):
                    inputs_map[name] = widget_values[w_idx]
                w_idx += 1
        prompt[node_id] = {
            "class_type": class_type,
            "inputs": inputs_map,
        }
    return prompt


def main():
    parser = argparse.ArgumentParser(description="Generate using workflow embedded in a PNG.")
    parser.add_argument("--png", required=True, type=Path, help="Path to PNG with workflow metadata.")
    parser.add_argument("--prompt", required=True, help="CLIP text prompt to inject.")
    parser.add_argument("--out", type=Path, default=None, help="Output PNG path (default image_<timestamp>.png).")
    parser.add_argument("--host", default="http://127.0.0.1:8188", help="ComfyUI API host.")
    parser.add_argument("--show", action="store_true", help="Display the resulting image after generation.")
    parser.add_argument("--debug", action="store_true", help="Print request payload before sending.")
    parser.add_argument("--seed", default=0, help="Seed value for KSampler (0=random, fixed=keep original, number=override).")
    parser.add_argument("--save-output", action="store_true", help="Save via embedder (default false uses preview only).")
    args = parser.parse_args()

    state = {"prompt_id": None}
    def handle_signal(signum, frame):
        print(f"[signal] Caught signal {signum}, attempting cancel")
        pid = state.get("prompt_id")
        if pid:
            cancel_prompt(args.host, pid)
        sys.exit(1)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except Exception:
            pass

    try:
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
        if args.out is None:
            ts = int(time.time())
            out_path = Path(f"image_{ts}.png")
        else:
            out_path = args.out
        out_path = download_image(args.host, result, out_path)

        if args.show:
            img = Image.open(out_path)
            img.show()
        else:
            print(f"Saved: {out_path.resolve()}")
        print(f"Prompt ID: {state['prompt_id']}")
    except KeyboardInterrupt:
        pid = state.get("prompt_id")
        if pid:
            cancel_prompt(args.host, pid)
        raise
    except Exception:
        pid = state.get("prompt_id")
        if pid:
            cancel_prompt(args.host, pid)
        raise


if __name__ == "__main__":
    main()
