import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigurationTests(unittest.TestCase):
    def test_workflow_links_and_gpu_cache(self):
        manifest = json.loads((ROOT / "models.json").read_text())
        pinned = {Path(x["path"]).name for x in manifest["files"]}
        pinned |= {Path(x["path"]).name for v in manifest["variants"].values() for x in v["files"]}
        paths = sorted((ROOT / "workflows").glob("*.api.json"))
        self.assertEqual([x.name for x in paths], ["qwen-image21-unsloth-q8.api.json", "qwen-image21.api.json"])
        for path in paths:
            with self.subTest(path.name):
                graph = json.loads(path.read_text())
                self.assertEqual(graph["5"]["inputs"]["device"], "gpu")
                self.assertIn(graph["1"]["inputs"]["model_name"], pinned)
                workflow = load("workflow", ROOT / "make_workflow.py").build(graph)
                self.assertEqual(len(workflow["nodes"]), len(graph))
                nodes = {node["id"]: node for node in workflow["nodes"]}
                for identifier, source, source_slot, target, target_slot, kind in workflow["links"]:
                    self.assertIn(identifier, nodes[source]["outputs"][source_slot]["links"])
                    self.assertEqual(nodes[target]["inputs"][target_slot]["link"], identifier)
                    self.assertEqual(nodes[source]["outputs"][source_slot]["type"], kind)
                committed = json.loads(path.with_name(path.name.replace(".api.json", ".ui.json")).read_text())
                self.assertEqual(committed, workflow)

    def test_manifest_is_pinned(self):
        manifest = json.loads((ROOT / "models.json").read_text())
        self.assertEqual(len(manifest["files"]), 4)
        self.assertEqual(sorted(manifest["variants"]), ["unsloth-q8_0"])
        for name, variant in manifest["variants"].items():
            self.assertTrue((ROOT / f"workflows/{variant['workflow']}.api.json").is_file(), name)
        for item in manifest["files"] + [x for v in manifest["variants"].values() for x in v["files"]]:
            self.assertRegex(item["revision"], r"^[a-f0-9]{40}$")
            self.assertRegex(item["sha256"], r"^[a-f0-9]{64}$")
            self.assertNotIn("..", Path(item["path"]).parts)
            self.assertFalse(Path(item["path"]).is_absolute())

    def test_downloader_preserves_unexpected_file(self):
        downloader = load("downloader", ROOT.parent / "download-qwen-image.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "qwen-image").mkdir()
            target = root / "models/qwen-image-2.1/test.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"keep")
            manifest = {"files": [{"path": "test.gguf", "size": 4, "sha256": "0" * 64}]}
            (root / "qwen-image/models.json").write_text(json.dumps(manifest))
            with patch.object(downloader, "ROOT", root), patch.object(downloader.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "wrong checksum"):
                    downloader.main([])
                run.assert_not_called()
            self.assertEqual(target.read_bytes(), b"keep")
            self.assertFalse((target.parent / "manifest.json").exists())

    def test_downloader_fetches_only_requested_variant(self):
        downloader = load("downloader", ROOT.parent / "download-qwen-image.py")
        digest = hashlib.sha256(b"x").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "qwen-image").mkdir()
            manifest = {"files": [{"repo": "r", "revision": "v", "file": "a", "path": "base.gguf",
                                   "size": 1, "sha256": digest}],
                        "variants": {"extra": {"workflow": "w", "files": [
                            {"repo": "r", "revision": "v", "file": "b", "path": "extra.gguf",
                             "size": 1, "sha256": digest}]}}}
            (root / "qwen-image/models.json").write_text(json.dumps(manifest))
            models = root / "models/qwen-image-2.1"

            def fetch(command, check):
                Path(command[command.index("--output") + 1]).write_bytes(b"x")

            with patch.object(downloader, "ROOT", root), patch.object(downloader.subprocess, "run", fetch):
                downloader.main([])
                self.assertFalse((models / "extra.gguf").exists())
                downloader.main(["--variant", "extra"])
            self.assertTrue((models / "extra.gguf").is_file())
            self.assertEqual(json.loads((models / "manifest.json").read_text()), manifest)

class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.args = types.SimpleNamespace(gpu_only=True)
        self.torch = types.SimpleNamespace(device=lambda x: x, cuda=Mock())
        self.torch.cuda.device_count.return_value = 2
        self.mm = types.ModuleType("comfy.model_management")
        self.mm.load_models_gpu = Mock()
        comfy = types.ModuleType("comfy")
        comfy.model_management = self.mm
        routes = types.SimpleNamespace(get=lambda path: lambda fn: fn)
        modules = {"torch": self.torch, "nodes": Mock(), "comfy": comfy,
                   "comfy.model_management": self.mm,
                   "comfy.cli_args": types.SimpleNamespace(args=self.args),
                   "aiohttp": types.SimpleNamespace(web=Mock()),
                   "server": types.SimpleNamespace(PromptServer=types.SimpleNamespace(
                       instance=types.SimpleNamespace(routes=routes)))}
        with patch.dict(sys.modules, modules):
            self.module = load("local_qwen", ROOT / "local_nodes/__init__.py")

    def patcher(self, devices, offload="cuda:0"):
        model = Mock()
        model.parameters.return_value = [types.SimpleNamespace(device=x) for x in devices]
        model.buffers.return_value = []
        return types.SimpleNamespace(model=model, load_device="cuda:0", offload_device=offload)

    def test_requires_two_gpus_and_gpu_only(self):
        self.module.require_gpu_mode()
        self.args.gpu_only = False
        with self.assertRaises(RuntimeError):
            self.module.require_gpu_mode()
        self.args.gpu_only = True
        self.torch.cuda.device_count.return_value = 1
        with self.assertRaises(RuntimeError):
            self.module.require_gpu_mode()

    def test_rejects_cpu_offload_before_loading(self):
        with self.assertRaisesRegex(RuntimeError, "Refusing"):
            self.module.fully_load(self.patcher(["cuda:0"], "cpu"), "cuda:0")
        self.mm.load_models_gpu.assert_not_called()

    def test_rejects_cpu_tensor_after_loading(self):
        with self.assertRaisesRegex(RuntimeError, "not entirely"):
            self.module.fully_load(self.patcher(["cuda:0", "cpu"]), "cuda:0")

    def test_serves_exactly_one_dit(self):
        manifest = ROOT / "models.json"
        base = self.module.served_dit(manifest, "base")
        self.assertEqual(base, {"variant": "base", "model_name": "qwen_image_2.1-Q6_K.gguf",
                                "workflow": "qwen-image21"})
        unsloth = self.module.served_dit(manifest, "unsloth-q8_0")
        self.assertEqual(unsloth["model_name"], "qwen-image-2.1-Q8_0.gguf")
        graph = json.loads((ROOT / f"workflows/{unsloth['workflow']}.api.json").read_text())
        self.assertEqual(graph["1"]["inputs"]["model_name"], unsloth["model_name"])
        with self.assertRaises(KeyError):
            self.module.served_dit(manifest, "missing")

    def test_accepts_full_gpu_model(self):
        model = self.patcher(["cuda:0"])
        self.module.fully_load(model, "cuda:0")
        self.mm.load_models_gpu.assert_called_once_with([model], force_full_load=True)
        self.assertEqual(len(self.module.verified_models), 1)


if __name__ == "__main__":
    unittest.main()
