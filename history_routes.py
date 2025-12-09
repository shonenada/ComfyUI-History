import json
import logging
from pathlib import Path

from aiohttp import web

import folder_paths
from history_utils import (
    PROMPTS_DIR,
    build_payload,
    list_prompt_files,
    safe_name,
    write_payload,
)

try:
    from server import PromptServer
except ImportError:
    PromptServer = None


def register_routes():
    if PromptServer is None:
        return

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
        for path in list_prompt_files():
            entry = {
                "name": path.name,
                "modified": path.stat().st_mtime,
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
        name = safe_name(request.match_info.get("name", ""))
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
