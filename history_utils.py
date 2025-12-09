import json
import logging
import re
from datetime import datetime
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def sanitize_prefix(prefix: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", prefix.strip())
    return safe or "prompt"


def extract_clip_texts(workflow):
    if not isinstance(workflow, dict):
        return []
    nodes = workflow.get("nodes", [])
    texts = []
    for node in nodes:
        try:
            if not isinstance(node, dict):
                logging.warning("PromptHistorySaver: unexpected node type %s", type(node))
                continue
            node_type = node.get("class_type") or node.get("type")
            if node_type not in {"CLIPTextEncode", "CLIPTextEncodeSDXL"}:
                continue
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                inputs = {}
            text_val = inputs.get("text")
            if not isinstance(text_val, str):
                wvals = node.get("widgets_values") or []
                if wvals and isinstance(wvals[0], str):
                    text_val = wvals[0]
            if isinstance(text_val, str):
                texts.append(
                    {
                        "id": node.get("id"),
                        "title": node.get("title"),
                        "text": text_val,
                        "type": node_type,
                    }
                )
        except Exception:
            logging.exception("PromptHistorySaver: failed extracting text from node: %s", node)
            continue
    return texts


def build_payload(prompt, workflow, mode, clip_only):
    payload = {"saved_at": datetime.now().isoformat(), "mode": mode}
    if not clip_only:
        if prompt is not None:
            payload["prompt"] = prompt
        if workflow is not None:
            payload["workflow"] = workflow
    payload["clip_texts"] = extract_clip_texts(workflow or prompt)
    if clip_only:
        payload["clip_only"] = True
        payload.pop("workflow", None)
        payload.pop("prompt", None)
        if not payload.get("clip_texts"):
            return None
    return payload


def write_payload(payload, file_prefix="prompt", suffix_extra=""):
    prompts_dir = PROMPTS_DIR
    sanitized_prefix = sanitize_prefix(file_prefix or "prompt")
    prompts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = prompts_dir / f"{sanitized_prefix}-{timestamp}{suffix_extra}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = prompts_dir / f"{sanitized_prefix}-{timestamp}-{counter}{suffix_extra}.json"

    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
    return path


def list_prompt_files():
    if not PROMPTS_DIR.exists():
        return []
    return sorted(
        [p for p in PROMPTS_DIR.glob("*.json") if p.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )


def safe_name(name: str):
    if not name:
        return None
    if re.fullmatch(r"[0-9A-Za-z._-]+\.json", name):
        return name
    return None
