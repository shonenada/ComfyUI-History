import json
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths
from history_utils import (
    PROMPTS_DIR,
    build_payload,
    extract_clip_texts,
    sanitize_prefix,
    write_payload,
)


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
        filename_prefix = sanitize_prefix(filename_prefix)
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
        if image is None or len(image) == 0:
            raise RuntimeError("No image data provided")
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
            return (data,)


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
