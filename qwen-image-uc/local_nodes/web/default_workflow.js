// The bundled frontend falls back to a Z-Image-Turbo graph whose models are not installed here.
// Replace that fallback once per page load with the tested workflow of this service.
import { app } from "../../scripts/app.js";

const NAME = "qwen-image21-uc.json";
let replaced = false;

const isBuiltinDefault = (graph) =>
  graph?.nodes?.some((node) => node.type === "UNETLoader"
    && node.widgets_values?.[0] === "z_image_turbo_bf16.safetensors");

app.registerExtension({
  name: "local.qwenImageUC.defaultWorkflow",
  async afterConfigureGraph() {
    const current = window.app;
    if (replaced || !isBuiltinDefault(current.graph.serialize())) return;
    replaced = true;
    const response = await fetch("/api/userdata/" + encodeURIComponent("workflows/" + NAME));
    if (!response.ok) return;
    const workflow = await response.json();
    setTimeout(() => current.loadGraphData(workflow, true, true, NAME), 0);
  },
});
