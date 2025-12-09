import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { ComfyDialog, $el } from "/scripts/ui.js";

console.log("[HistoryLoader] script loaded");

const dialog = new ComfyDialog();
dialog.element.classList.add("prompt-history-dialog");

async function fetchEntries() {
    console.log("[HistoryLoader] fetching entries");
    const resp = await api.fetchApi("/history/prompts");
    if (!resp.ok) {
        throw new Error(`Failed to list prompts (${resp.status})`);
    }
    return resp.json();
}

async function fetchPrompt(name) {
    console.log("[HistoryLoader] fetching prompt", name);
    const resp = await api.fetchApi(`/history/prompts/${name}`);
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Failed to load prompt (${resp.status})`);
    }
    return resp.json();
}

async function saveNow(options = {}) {
    const workflow = app.graph?.serialize?.();
    if (!workflow) {
        throw new Error("No workflow available to save.");
    }
    const body = {
        workflow,
        prompt: { workflow },
        clip_only: !!options.clip_only,
        file_prefix: options.file_prefix || "prompt",
        clip_suffix: !!options.clip_suffix,
    };
    console.log("[HistoryLoader] saveNow body", body);
    const resp = await api.fetchApi("/history/save_now", {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "Content-Type": "application/json" },
    });
    if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Failed to save (${resp.status})`);
    }
    const data = await resp.json();
    return data.path || data.saved;
}

function parsePngTexts(uint8) {
    const texts = {};
    let offset = 8; // skip signature
    const readUint32 = (pos) => (uint8[pos] << 24) | (uint8[pos + 1] << 16) | (uint8[pos + 2] << 8) | uint8[pos + 3];
    while (offset + 8 <= uint8.length) {
        const length = readUint32(offset);
        const type = String.fromCharCode(uint8[offset + 4], uint8[offset + 5], uint8[offset + 6], uint8[offset + 7]);
        const dataStart = offset + 8;
        const dataEnd = dataStart + length;
        if (dataEnd > uint8.length) break;
        if (type === "tEXt") {
            const data = uint8.slice(dataStart, dataEnd);
            const nullIdx = data.indexOf(0);
            if (nullIdx >= 0) {
                const keyword = new TextDecoder().decode(data.slice(0, nullIdx));
                const text = new TextDecoder().decode(data.slice(nullIdx + 1));
                texts[keyword] = text;
            }
        }
        offset = dataEnd + 4; // skip CRC
    }
    return texts;
}

function createFilePicker({ accept = ".png", multiple = false, onFiles }) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.multiple = multiple;
    input.style.display = "none";
    input.addEventListener("change", (e) => {
        const files = Array.from(e.target.files || []);
        if (files.length && onFiles) {
            onFiles(files);
        }
        input.remove();
    });
    document.body.appendChild(input);
    input.click();
}

function readPngWorkflow(file, { which = "workflow" } = {}) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            try {
                const arr = new Uint8Array(reader.result);
                const texts = parsePngTexts(arr);
                const key = which === "prompt" ? "prompt" : "workflow";
                if (!(key in texts)) {
                    reject(new Error(`No '${key}' metadata found in PNG.`));
                    return;
                }
                resolve(texts[key]);
            } catch (err) {
                reject(err);
            }
        };
        reader.onerror = () => reject(reader.error || new Error("Failed to read file."));
        reader.readAsArrayBuffer(file);
    });
}

async function loadWorkflowFromPng() {
    createFilePicker({
        accept: ".png",
        multiple: false,
        onFiles: async (files) => {
            const file = files[0];
            if (!file) return;
            try {
                const raw = await readPngWorkflow(file, { which: "workflow" });
                let parsed;
                try {
                    parsed = JSON.parse(raw);
                } catch {
                    throw new Error("Workflow metadata is not valid JSON.");
                }
                await app.loadGraphData(parsed);
                console.log("[HistoryLoader] loaded workflow from PNG", file.name);
            } catch (err) {
                console.error("[HistoryLoader] failed loading workflow from PNG", err);
                alert(`Load failed: ${err.message}`);
            }
        },
    });
}

async function loadWorkflowFromRemotePng() {
    try {
        const resp = await api.fetchApi("/history/output_images");
        if (!resp.ok) {
            throw new Error(`Failed to list output images (${resp.status})`);
        }
        const files = await resp.json();
        if (!files.length) {
            alert("No PNG files found in output.");
            return;
        }
        const chooser = new ComfyDialog();
        chooser.element.style.width = "480px";
        chooser.element.style.padding = "16px";
        chooser.element.style.color = "var(--fg-color)";

    const list = $el(
        "select",
        {
            size: Math.min(14, files.length),
            style: {
                width: "100%",
                boxSizing: "border-box",
                backgroundColor: "var(--comfy-input-bg)",
                color: "var(--fg-color)",
                marginBottom: "12px",
            },
        },
        files.map((f, idx) =>
            $el("option", { value: idx }, `${f.path || f.name} (${Math.round(f.size / 1024)} KB)`)
        )
    );

        const status = $el("div", { style: { minHeight: "18px", color: "var(--fg-color-muted)", marginBottom: "12px" } });

        const loadBtn = $el(
            "button",
            {
                textContent: "Load",
                onclick: async () => {
                    const idx = parseInt(list.value, 10);
                    if (Number.isNaN(idx) || idx < 0 || idx >= files.length) {
                        status.textContent = "Invalid selection.";
                        return;
                    }
                    const relPath = files[idx].path || files[idx].name;
                    status.textContent = "Fetching PNG...";
                    try {
                        const imgResp = await api.fetchApi(`/history/output_images/${encodeURIComponent(relPath)}`);
                        if (!imgResp.ok) {
                            const text = await imgResp.text();
                            throw new Error(text || `Failed to fetch image (${imgResp.status})`);
                        }
                        const blob = await imgResp.blob();
                        const arrBuf = await blob.arrayBuffer();
                        const arr = new Uint8Array(arrBuf);
                        const texts = parsePngTexts(arr);
                        const raw = texts.workflow;
                        if (!raw) {
                            throw new Error("No 'workflow' metadata found in PNG.");
                        }
                        let parsed;
                        try {
                            parsed = JSON.parse(raw);
                        } catch {
                            throw new Error("Workflow metadata is not valid JSON.");
                        }
                        await app.loadGraphData(parsed);
                        console.log("[HistoryLoader] loaded workflow from remote PNG", relPath);
                        chooser.close();
                    } catch (err) {
                        console.error("[HistoryLoader] failed loading workflow from remote PNG", err);
                        status.textContent = err.message;
                    }
                },
            },
            []
        );

        const closeBtn = $el(
            "button",
            {
                textContent: "Close",
                onclick: () => chooser.close(),
            },
            []
        );

        chooser.element.replaceChildren(
            $el("div", {}, [
                $el("div", { style: { marginBottom: "8px" } }, "Select a PNG from output:"),
                list,
                status,
                $el("div", {}, [loadBtn, closeBtn]),
            ])
        );
        chooser.show();
    } catch (err) {
        console.error("[HistoryLoader] failed loading workflow from remote PNG", err);
        alert(`Load failed: ${err.message}`);
    }
}

function buildDialog(entries = []) {
    console.log("[HistoryLoader] building dialog");
    dialog.element.style.width = "440px";
    dialog.element.style.padding = "16px";

    const info = $el("div", { style: { marginBottom: "8px", color: "var(--fg-color)" } }, [
        "Select a saved prompt to load its workflow into the canvas.",
    ]);

    const list = $el(
        "select",
        {
            size: 10,
            style: {
                width: "100%",
                boxSizing: "border-box",
                backgroundColor: "var(--comfy-input-bg)",
                color: "var(--fg-color)",
                marginBottom: "12px",
            },
        },
        entries.map((entry) => {
            const labelParts = [entry.name];
            if (entry.saved_at) {
                labelParts.push(`@ ${entry.saved_at}`);
            } else if (entry.modified) {
                labelParts.push(`@ ${entry.modified}`);
            }
            if (entry.mode) {
                labelParts.push(`(${entry.mode})`);
            }
            if (entry.clip_only) {
                labelParts.push("[clip-only]");
            } else if (entry.clip_count) {
                labelParts.push(`[clips:${entry.clip_count}]`);
            }
            return $el("option", { value: entry.name }, labelParts.join(" "));
        })
    );

    const status = $el("div", {
        style: { minHeight: "18px", color: "var(--fg-color-muted)", marginBottom: "12px" },
    });

    const refreshBtn = $el(
        "button",
        {
            textContent: "Refresh",
            style: { marginRight: "8px" },
            onclick: async () => {
                status.textContent = "Loading...";
                try {
                    const updated = await fetchEntries();
                    dialog.close();
                    showDialog(updated);
                } catch (err) {
                    status.textContent = err.message;
                }
            },
        },
        []
    );

    const loadBtn = $el(
        "button",
        {
            textContent: "Load",
            style: { marginRight: "8px" },
            onclick: async () => {
                const selected = list.value;
                if (!selected) {
                    status.textContent = "Pick a prompt first.";
                    return;
                }
                status.textContent = "Loading prompt...";
                try {
                    const data = await fetchPrompt(selected);
                    const workflow = data.workflow || data?.prompt?.workflow || data.prompt;
                    if (!workflow) {
                        throw new Error("No workflow found in saved prompt file.");
                    }
                    await app.loadGraphData(workflow);
                    dialog.close();
                } catch (err) {
                    status.textContent = err.message;
                }
            },
        },
        []
    );

    const loadTextBtn = $el(
        "button",
        {
            textContent: "Load Text",
            style: { marginRight: "8px" },
            onclick: async () => {
                const selected = list.value;
                if (!selected) {
                    status.textContent = "Pick a prompt first.";
                    return;
                }
                status.textContent = "Loading texts...";
                try {
                    const data = await fetchPrompt(selected);
                    const clipTexts = data.clip_texts || [];
                    if (!clipTexts.length) {
                        throw new Error("No CLIP text data in this save.");
                    }
                    let applied = 0;
                    let missing = 0;
                    clipTexts.forEach((entry) => {
                        const node = app.graph.getNodeById(entry.id);
                        if (!node || !node.widgets) {
                            missing++;
                            return;
                        }
                        const textWidget = node.widgets.find((w) => w.name === "text");
                        if (textWidget) {
                            textWidget.value = entry.text;
                            applied++;
                        } else {
                            missing++;
                        }
                    });
                    app.graph.setDirtyCanvas(true, true);
                    status.textContent = `Applied ${applied} text prompt(s); ${missing} missing.`;
                } catch (err) {
                    status.textContent = err.message;
                }
            },
        },
        []
    );

    const closeBtn = $el(
        "button",
        {
            textContent: "Close",
            onclick: () => dialog.close(),
        },
        []
    );

    dialog.element.replaceChildren(
        $el(
            "div",
            { style: { color: "var(--fg-color)" } },
            [info, list, status, $el("div", {}, [refreshBtn, loadBtn, loadTextBtn, closeBtn])]
        )
    );
}

async function showDialog(entries) {
    buildDialog(entries);
    dialog.show();
}

async function openHistoryDialog() {
    try {
        const entries = await fetchEntries();
        await showDialog(entries);
    } catch (err) {
        dialog.element.replaceChildren(
            $el("div", { style: { padding: "16px", color: "var(--fg-color)" } }, [
                "Failed to load prompt history:",
                $el("div", { style: { marginTop: "8px" } }, err.message),
            ])
        );
        dialog.show();
    }
}

function addMenuButton(attempt = 0) {
    console.log("[HistoryLoader] adding menu button, attempt", attempt);
    const menu = document.querySelector(".comfy-menu");
    if (!menu) {
        if (attempt < 20) {
            setTimeout(() => addMenuButton(attempt + 1), 250);
        }
        return;
    }

    const existing = document.querySelector("#prompt-history-load");
    const existingSave = document.querySelector("#prompt-history-save");
    const existingSaveText = document.querySelector("#prompt-history-save-text");
    const existingLoadPng = document.querySelector("#prompt-history-load-png");
    const existingLoadRemote = document.querySelector("#prompt-history-load-remote");

    const historyBtn = existing
        ? existing
        : $el("button", {
              id: "prompt-history-load",
              className: "comfyui-button comfyui-menu-mobile-collapse",
              textContent: "History",
              style: { marginLeft: "6px" },
              onclick: openHistoryDialog,
          });

    const saveBtn = existingSave
        ? existingSave
        : $el("button", {
              id: "prompt-history-save",
              className: "comfyui-button comfyui-menu-mobile-collapse",
              textContent: "Save Now",
              style: { marginLeft: "6px" },
              onclick: async () => {
                  try {
                      await saveNow({ clip_only: false });
                      console.log("[HistoryLoader] save_now complete");
                  } catch (err) {
                      console.error("[HistoryLoader] save_now failed", err);
                      alert(`Save failed: ${err.message}`);
                  }
              },
          });

    const saveTextBtn = existingSaveText
        ? existingSaveText
        : $el("button", {
              id: "prompt-history-save-text",
              className: "comfyui-button comfyui-menu-mobile-collapse",
              textContent: "Save Text",
              style: { marginLeft: "6px" },
              onclick: async () => {
                  try {
                      await saveNow({ clip_only: true, clip_suffix: true });
                      console.log("[HistoryLoader] save_text complete");
                  } catch (err) {
                      console.error("[HistoryLoader] save_text failed", err);
                      alert(`Save Text failed: ${err.message}`);
                  }
              },
          });

    const loadPngBtn = existingLoadPng
        ? existingLoadPng
        : $el("button", {
              id: "prompt-history-load-png",
              className: "comfyui-button comfyui-menu-mobile-collapse",
              textContent: "Load PNG",
              style: { marginLeft: "6px" },
              onclick: loadWorkflowFromPng,
          });

    const loadRemoteBtn = existingLoadRemote
        ? existingLoadRemote
        : $el("button", {
              id: "prompt-history-load-remote",
              className: "comfyui-button comfyui-menu-mobile-collapse",
              textContent: "Load Remote PNG",
              style: { marginLeft: "6px" },
              onclick: loadWorkflowFromRemotePng,
          });

    const menuDisplay = getComputedStyle(menu).display;
    if (menuDisplay !== "none") {
        if (!existing) menu.appendChild(historyBtn);
        if (!existingSave) menu.appendChild(saveBtn);
        if (!existingSaveText) menu.appendChild(saveTextBtn);
        if (!existingLoadPng) menu.appendChild(loadPngBtn);
        if (!existingLoadRemote) menu.appendChild(loadRemoteBtn);
        console.log("[HistoryLoader] added buttons to .comfy-menu");
        return;
    }

    // Fallback: floating buttons if the menu is hidden (e.g., mobile layout or theme hides it)
    const floatContainerId = "prompt-history-fab";
    let floatContainer = document.getElementById(floatContainerId);
    if (!floatContainer) {
        floatContainer = $el(
            "div",
            {
                id: floatContainerId,
                style: {
                    position: "fixed",
                    top: "10px",
                    right: "10px",
                    zIndex: 10000,
                    display: "flex",
                    gap: "8px",
                },
            },
            []
        );
        document.body.appendChild(floatContainer);
    }
    if (!existing) floatContainer.appendChild(historyBtn);
    if (!existingSave) floatContainer.appendChild(saveBtn);
    if (!existingSaveText) floatContainer.appendChild(saveTextBtn);
    if (!existingLoadPng) floatContainer.appendChild(loadPngBtn);
    if (!existingLoadRemote) floatContainer.appendChild(loadRemoteBtn);
    console.log("[HistoryLoader] menu hidden, added floating History/Save buttons");
}

app.registerExtension({
    name: "ComfyUI.HistoryLoader",
    setup() {
        console.log("[HistoryLoader] setup called");
        addMenuButton();
    },
});
