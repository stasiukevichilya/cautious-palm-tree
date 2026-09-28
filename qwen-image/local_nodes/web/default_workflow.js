// The bundled frontend falls back to a Z-Image-Turbo graph whose models are not installed here.
// Replace that fallback once per page load with the tested workflow for the served DiT.
import { app } from "../../scripts/app.js";

let replaced = false;

const isBuiltinDefault = (graph) =>
  graph?.nodes?.some((node) => node.type === "UNETLoader"
    && node.widgets_values?.[0] === "z_image_turbo_bf16.safetensors");

app.registerExtension({
  name: "local.qwenImage.defaultWorkflow",
  async afterConfigureGraph() {
    const current = window.app;
    if (replaced || !isBuiltinDefault(current.graph.serialize())) return;
    replaced = true;
    const config = await fetch("/local-qwen-image/config");
    if (!config.ok) return;
    const name = (await config.json()).workflow + ".json";
    const response = await fetch("/api/userdata/" + encodeURIComponent("workflows/" + name));
    if (!response.ok) return;
    const workflow = await response.json();
    setTimeout(() => current.loadGraphData(workflow, true, true, name), 0);
  },
});
