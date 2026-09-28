"""Generate an editable ComfyUI workflow from the tested API graph."""
import json
from pathlib import Path


def build(graph):
    layout = {
        "1": ([], ["MODEL"], ["model_name"], [0, 0]),
        "2": ([], ["CLIP"], ["encoder_name"], [0, 200]),
        "3": ([], ["VAE"], ["vae_name"], [0, 400]),
        "4": (["clip", "vae"], ["CONDITIONING", "CONDITIONING", "LATENT"],
              ["prompt", "negative_prompt", "resolution"], [370, 0]),
        "5": (["model"], ["MODEL"], ["device", "dtype"], [370, 470]),
        "6": (["model", "positive", "negative", "latent_image"], ["LATENT"],
              ["seed", "@randomize", "steps", "cfg", "sampler_name", "scheduler", "denoise"], [780, 0]),
        "7": (["samples", "vae"], ["IMAGE"],
              ["tile_size", "overlap", "temporal_size", "temporal_overlap"], [1140, 0]),
        "8": (["images"], [], ["filename_prefix"], [1480, 0]),
    }
    nodes, links = [], []
    for key, (inputs, outputs, widgets, pos) in layout.items():
        spec = graph[key]
        ports = []
        for slot, name in enumerate(inputs):
            source, source_slot = spec["inputs"][name]
            kind = layout[source][1][source_slot]
            link_id = len(links) + 1
            links.append([link_id, int(source), source_slot, int(key), slot, kind])
            ports.append({"name": name, "type": kind, "link": link_id})
        nodes.append({"id": int(key), "type": spec["class_type"], "pos": pos,
                      "size": [370 if key == "4" else 330, 340 if key in ("4", "6", "8") else 150],
                      "flags": {}, "order": int(key) - 1, "mode": 0,
                      "inputs": ports, "outputs": [{"name": x, "type": x, "links": []} for x in outputs],
                      "properties": {"Node name for S&R": spec["class_type"]},
                      "widgets_values": [x[1:] if x.startswith("@") else spec["inputs"][x] for x in widgets]})
    by_id = {node["id"]: node for node in nodes}
    for link_id, source, slot, *_ in links:
        by_id[source]["outputs"][slot]["links"].append(link_id)
    return {"version": 0.4, "last_node_id": 8, "last_link_id": len(links),
            "nodes": nodes, "links": links, "groups": [], "config": {}, "extra": {}}


if __name__ == "__main__":
    directory = Path(__file__).parent / "workflows"
    for path in directory.glob("*.api.json"):
        graph = json.loads(path.read_text())
        target = directory / path.name.replace(".api.json", ".ui.json")
        target.write_text(json.dumps(build(graph), indent=2) + "\n")
