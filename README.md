# ComfyUI-History

Custom nodes and UI helpers for capturing, saving, and restoring ComfyUI workflows and prompts.

## Nodes

- **Prompt History Saver**: Auto/Manual save of the current prompt/workflow (and CLIP texts) into `ComfyUI/prompts`. Supports text-only saves.
- **Embed Prompt to PNG**: Saves images while embedding the full workflow/prompt metadata into PNG text chunks.
- **Load Prompt from PNG**: Reads workflow/prompt metadata from a PNG saved by the embedder and outputs it as JSON text.

## Frontend buttons

The extension injects buttons into the top menu (or a floating bar if the menu is hidden):

- `History`: Open the history dialog to load saved prompts or reapply CLIP texts.
- `Save Now`: Save the current workflow to `prompts/`.
- `Save Text`: Save only CLIP text prompts to `prompts/` with `_CLIP.json` suffix.
- `Load PNG`: Pick a local PNG file and load its embedded workflow.
- `Load Remote PNG`: Select a PNG from the `output` directory (recurses into subfolders) and load its embedded workflow.

## Backend routes

- `GET /history/prompts` — list saved prompt JSON files.
- `GET /history/prompts/{name}` — fetch a saved prompt JSON.
- `POST /history/save_now` — save current workflow/clip-only payload (used by “Save Now”/“Save Text” buttons).
- `GET /history/output_images` — list PNG files under the `output` directory (recursive).
- `GET /history/output_images/{path}` — fetch a PNG by relative path from `output`.

## Notes

- Saved prompt filenames follow `<prefix>-<timestamp>.json` (or `_CLIP` suffix for text-only).
- The loader buttons rely on the embedded workflow stored in PNG metadata by the embedder node.
