import json
import re
from datetime import datetime
from pathlib import Path
import logging

from aiohttp import web

try:
    from server import PromptServer
except ImportError:
    PromptServer = None

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class PromptHistorySaver:
    """Saves the current prompt/workflow into the ComfyUI/prompts folder."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "auto_save": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Save the prompt on every generation that reaches this node.",
                    },
                ),
            },
            "optional": {
                "file_prefix": (
                    "STRING",
                    {
                        "default": "prompt",
                        "tooltip": "Prefix for saved files. Non-alphanumeric characters become underscores.",
                    },
                ),
                "save_clip_text_only": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "Only store CLIP text inputs (no workflow).",
                    },
                ),
                "save_now": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "widgetType": "BUTTON",
                        "label_on": "Saved",
                        "label_off": "Save current prompt",
                        "tooltip": "Click to write the prompt immediately, even if auto-save is off.",
                    },
                ),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_history"
    OUTPUT_NODE = True
    CATEGORY = "history"

    def __init__(self):
        self.prompts_dir = PROMPTS_DIR

    @staticmethod
    def _sanitize_prefix(prefix: str) -> str:
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", prefix.strip())
        return safe or "prompt"

    def _build_payload(self, prompt, workflow):
        payload = {"saved_at": datetime.now().isoformat()}
        if prompt is not None:
            payload["prompt"] = prompt
        if workflow is not None:
            payload["workflow"] = workflow
        payload["clip_texts"] = self._extract_clip_texts(workflow or prompt)
        return payload

    @staticmethod
    def _extract_clip_texts(workflow):
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

    def _next_path(self, prefix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.prompts_dir / f"{prefix}-{timestamp}.json"
        counter = 1
        while path.exists():
            counter += 1
            path = self.prompts_dir / f"{prefix}-{timestamp}-{counter}.json"
        return path

    def save_history(self, auto_save=True, file_prefix="prompt", save_clip_text_only=False, save_now=False, prompt=None, extra_pnginfo=None):
        should_save = bool(auto_save) or bool(save_now)
        if not should_save:
            return {"ui": {"text": ("History not saved (auto-save disabled, manual save not triggered).",)}}

        workflow = None
        if isinstance(extra_pnginfo, dict):
            workflow = extra_pnginfo.get("workflow")
        if workflow is None and isinstance(prompt, dict):
            workflow = prompt.get("workflow")

        if prompt is None and workflow is None:
            return {"ui": {"text": ("No prompt data available to save.",)}}

        payload = build_payload(
            prompt if not save_clip_text_only else None,
            None if save_clip_text_only else workflow,
            "manual" if save_now else "auto",
            save_clip_text_only,
        )
        if payload is None:
            return {"ui": {"text": ("No CLIP text inputs found to save.",)}}

        path = write_payload(payload, file_prefix)

        message = f"Saved prompt to {path}"
        return {"ui": {"text": (message,)}}


class PromptWorkflowEmbedder:
    """Saves images with the current workflow embedded in PNG metadata."""

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "Images to save with workflow metadata embedded."}),
                "filename_prefix": ("STRING", {"default": "PromptEmbed", "tooltip": "Filename prefix for saved PNGs."}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = "history"
    DESCRIPTION = "Saves PNGs that include the full workflow/prompt in metadata."

    def _metadata(self, prompt, extra_pnginfo):
        metadata = PngInfo()
        workflow = None
        if isinstance(extra_pnginfo, dict):
            workflow = extra_pnginfo.get("workflow")
        if workflow is None and isinstance(prompt, dict):
            workflow = prompt.get("workflow")
        if prompt is not None:
            try:
                metadata.add_text("prompt", json.dumps(prompt))
            except Exception:
                logging.exception("PromptWorkflowEmbedder: failed to serialize prompt")
        if workflow is not None and (not extra_pnginfo or "workflow" not in extra_pnginfo):
            try:
                metadata.add_text("workflow", json.dumps(workflow))
            except Exception:
                logging.exception("PromptWorkflowEmbedder: failed to serialize workflow")
        if isinstance(extra_pnginfo, dict):
            for k, v in extra_pnginfo.items():
                try:
                    metadata.add_text(k, json.dumps(v))
                except Exception:
                    logging.exception("PromptWorkflowEmbedder: failed to add extra_pnginfo key %s", k)
        return metadata

    def save_images(self, images, filename_prefix="PromptEmbed", prompt=None, extra_pnginfo=None):
        filename_prefix = PromptHistorySaver._sanitize_prefix(filename_prefix)
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0]
        )
        meta = self._metadata(prompt, extra_pnginfo)
        results = []
        for batch_number, image in enumerate(images):
            arr = 255.0 * image.cpu().numpy()
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
            filename_with_batch_num = filename.replace("%batch_num%", str(batch_number))
            file = f"{filename_with_batch_num}_{counter:05}_.png"
            img.save(Path(full_output_folder) / file, pnginfo=meta)
            results.append({"filename": file, "subfolder": subfolder, "type": self.type})
            counter += 1
        return {"ui": {"images": results}}


class PromptWorkflowLoader:
    """Loads a workflow embedded in a PNG and outputs it for use."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {"tooltip": "PNG image that has workflow metadata embedded."}),
            },
            "optional": {
                "which": (
                    "STRING",
                    {
                        "default": "workflow",
                        "tooltip": "Metadata key to read: 'workflow' or 'prompt'.",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("workflow_json",)
    FUNCTION = "load_workflow"
    CATEGORY = "history"
    DESCRIPTION = "Reads workflow/prompt metadata from a PNG saved with PromptWorkflowEmbedder."

    def load_workflow(self, image, which="workflow"):
        # image is torch tensor BCHW in Comfy; convert first in batch to PIL to inspect metadata
        if image is None or len(image) == 0:
            raise RuntimeError("No image data provided")
        # Convert first image to PIL and read info
        arr = 255.0 * image[0].cpu().numpy()
        pil_img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        info = pil_img.info or {}
        key = "workflow" if which != "prompt" else "prompt"
        data = info.get(key)
        if data is None:
            raise RuntimeError(f"No '{key}' metadata found in PNG.")
        try:
            return (json.dumps(json.loads(data), indent=2),)
        except Exception:
            # If already serialized as string, return as-is
            return (data,)


def _list_prompt_files():
    if not PROMPTS_DIR.exists():
        return []
    return sorted(
        [p for p in PROMPTS_DIR.glob("*.json") if p.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )


def _safe_name(name: str):
    if not name:
        return None
    if re.fullmatch(r"[0-9A-Za-z._-]+\.json", name):
        return name
    return None


def build_payload(prompt, workflow, mode, clip_only):
    payload = {"saved_at": datetime.now().isoformat(), "mode": mode}
    if not clip_only:
        if prompt is not None:
            payload["prompt"] = prompt
        if workflow is not None:
            payload["workflow"] = workflow
    payload["clip_texts"] = PromptHistorySaver._extract_clip_texts(workflow or prompt)
    if clip_only:
        payload["clip_only"] = True
        payload.pop("workflow", None)
        payload.pop("prompt", None)
        if not payload.get("clip_texts"):
            return None
    return payload


def write_payload(payload, file_prefix="prompt", suffix_extra=""):
    prompts_dir = PROMPTS_DIR
    sanitized_prefix = PromptHistorySaver._sanitize_prefix(file_prefix or "prompt")
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


if PromptServer is not None:

    @PromptServer.instance.routes.get("/history/output_images")
    async def list_output_images(request):
        output_dir = Path(folder_paths.get_output_directory())
        files = []
        for p in sorted(output_dir.rglob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                stat = p.stat()
                rel = p.relative_to(output_dir)
                files.append({"name": p.name, "path": str(rel), "size": stat.st_size, "mtime": stat.st_mtime})
            except Exception:
                logging.exception("PromptHistorySaver: failed stat on %s", p)
        return web.json_response(files)

    @PromptServer.instance.routes.get("/history/output_images/{name:.*}")
    async def get_output_image(request):
        name = request.match_info.get("name", "")
        if not name:
            raise web.HTTPBadRequest(text="Invalid filename")
        output_dir = Path(folder_paths.get_output_directory()).resolve()
        path = (output_dir / name).resolve()
        try:
            path.relative_to(output_dir)
        except Exception:
            raise web.HTTPBadRequest(text="Invalid filename")
        if not path.is_file():
            raise web.HTTPNotFound()
        try:
            data = path.read_bytes()
        except Exception:
            logging.exception("PromptHistorySaver: failed to read output image %s", path)
            raise web.HTTPBadRequest(text="Failed to read output image")
        return web.Response(body=data, headers={"Content-Type": "image/png"})

    @PromptServer.instance.routes.get("/history/prompts")
    async def list_prompts(request):
        entries = []
        for path in _list_prompt_files():
            entry = {
                "name": path.name,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            }
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                entry["saved_at"] = data.get("saved_at")
                entry["mode"] = data.get("mode")
                entry["clip_only"] = data.get("clip_only", False)
                if isinstance(data.get("clip_texts"), list):
                    entry["clip_count"] = len(data["clip_texts"])
            except Exception:
                logging.exception("PromptHistorySaver: failed reading prompt file %s", path)
                entry["error"] = "unreadable"
            entries.append(entry)
        return web.json_response(entries)

    @PromptServer.instance.routes.get("/history/prompts/{name}")
    async def get_prompt(request):
        name = _safe_name(request.match_info.get("name", ""))
        if name is None:
            raise web.HTTPBadRequest(text="Invalid filename")
        path = PROMPTS_DIR / name
        if not path.is_file():
            raise web.HTTPNotFound()
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logging.exception("PromptHistorySaver: failed to read prompt file %s", path)
            raise web.HTTPBadRequest(text="Failed to read prompt file")
        return web.json_response(data)

    @PromptServer.instance.routes.post("/history/save_now")
    async def save_now(request):
        try:
            body = await request.json()
        except Exception:
            logging.exception("PromptHistorySaver: invalid JSON body")
            raise web.HTTPBadRequest(text="Invalid JSON")

        workflow = body.get("workflow")
        prompt = body.get("prompt")
        clip_only = bool(body.get("clip_only", False))
        file_prefix = body.get("file_prefix", "prompt")
        clip_suffix = bool(body.get("clip_suffix", False))
        payload = build_payload(prompt, workflow, "manual", clip_only)
        if payload is None:
            raise web.HTTPBadRequest(text="No CLIP text inputs found to save.")
        suffix_extra = "_CLIP" if clip_suffix else ""
        path = write_payload(payload, file_prefix, suffix_extra=suffix_extra)
        return web.json_response({"saved": path.name, "path": str(path)})

NODE_CLASS_MAPPINGS = {
    "PromptHistorySaver": PromptHistorySaver,
    "PromptWorkflowEmbedder": PromptWorkflowEmbedder,
    "PromptWorkflowLoader": PromptWorkflowLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptHistorySaver": "Prompt History Saver",
    "PromptWorkflowEmbedder": "Embed Prompt to PNG",
    "PromptWorkflowLoader": "Load Prompt from PNG",
}

# Tell ComfyUI where to load frontend assets for this extension.
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
