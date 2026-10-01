import importlib.util
import json
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
# Model card: Q4_K_M DiT (recommended), INT8 ConvRot text encoder (lower memory), BF16 VAE.
CARD = {
    "qwen-image-2.1-UC-Q4_K_M.gguf": "e79c8a009f2ecbdb6c70fd663d9aea9ee304a0d91f347e4169a756b8ad141b41",
    "text_encoders/qwen3vl_8b_int8_convrot.safetensors":
        "8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f",
    "vae/qwen_image_2.1_vae_bf16.safetensors": "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9",
}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigurationTests(unittest.TestCase):
    def test_manifest_matches_model_card(self):
        manifest = json.loads((ROOT / "models.json").read_text())
        self.assertEqual({x["file"]: x["sha256"] for x in manifest["files"]}, CARD)
        for item in manifest["files"]:
            self.assertEqual(item["repo"], "abenzerps/Qwen-Image-2.1-Uncensored-GGUF")
            self.assertRegex(item["revision"], r"^[a-f0-9]{40}$")
            self.assertNotIn("..", Path(item["path"]).parts)

    def test_workflow_follows_official_template(self):
        graph = json.loads((ROOT / "workflows/qwen-image21-uc.api.json").read_text())
        manifest = {Path(x["path"]).name for x in json.loads((ROOT / "models.json").read_text())["files"]}
        self.assertEqual({graph[k]["inputs"][n] for k, n in (("1", "model_name"), ("2", "encoder_name"),
                                                             ("3", "vae_name"))}, manifest)
        sampler = graph["6"]["inputs"]
        self.assertEqual((sampler["steps"], sampler["cfg"], sampler["sampler_name"], sampler["scheduler"]),
                         (25, 1.0, "euler", "simple"))
        self.assertEqual(graph["7"]["class_type"], "VAEDecode")
        self.assertEqual(graph["5"]["inputs"]["device"], "gpu")  # KV cache stays in VRAM, never "auto"
        workflow = load("workflow", ROOT / "make_workflow.py").build(graph)
        nodes = {node["id"]: node for node in workflow["nodes"]}
        self.assertEqual(len(nodes), len(graph))
        for identifier, source, source_slot, target, target_slot, kind in workflow["links"]:
            self.assertIn(identifier, nodes[source]["outputs"][source_slot]["links"])
            self.assertEqual(nodes[target]["inputs"][target_slot]["link"], identifier)
        self.assertEqual(json.loads((ROOT / "workflows/qwen-image21-uc.ui.json").read_text()), workflow)

    def test_service_is_isolated_from_the_bot(self):
        compose = (ROOT.parent / "compose.yaml").read_text()
        service = compose.split("\n  qwen-image-uc:\n", 1)[1].split("\n  tgbot:\n", 1)[0]
        self.assertIn("networks: [qwen-image-uc]", service)
        self.assertIn('"127.0.0.1:8085:8188"', service)
        bot = compose.split("\n  tgbot:\n", 1)[1].split("\n  sdxl:\n", 1)[0]
        self.assertNotIn("qwen-image-uc", bot.replace("./qwen-image/workflows", ""))
        # Docker Desktop does not isolate bridge networks, so the bot pins its backends to service names.
        settings = (ROOT.parent / "tgbot/settings.py").read_text()
        allowed = settings.split("ALLOWED_BACKENDS = ", 1)[1].split("\n", 1)[0]
        self.assertEqual(allowed, '{"llm_backends": {"qwen"}, "image_backends": {"sdxl", "qwen-image"}}')
        self.assertIn("if host != name:", settings)


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.args = types.SimpleNamespace(highvram=True, gpu_only=False)
        self.torch = types.SimpleNamespace(device=lambda x: x, cuda=Mock())
        self.torch.cuda.device_count.return_value = 1
        self.mm = types.ModuleType("comfy.model_management")
        self.mm.load_models_gpu = Mock()
        comfy = types.ModuleType("comfy")
        comfy.model_management = self.mm
        routes = types.SimpleNamespace(get=lambda path: lambda fn: fn)
        self.nodes = Mock()
        modules = {"torch": self.torch, "nodes": self.nodes, "comfy": comfy, "comfy.model_management": self.mm,
                   "comfy.cli_args": types.SimpleNamespace(args=self.args),
                   "aiohttp": types.SimpleNamespace(web=Mock()),
                   "server": types.SimpleNamespace(PromptServer=types.SimpleNamespace(
                       instance=types.SimpleNamespace(routes=routes))),
                   "prometheus_client": MetricsTests.fake_prometheus_client()}
        with patch.dict(sys.modules, modules):
            self.module = load("local_qwen_uc", ROOT / "local_nodes/__init__.py")

    def patcher(self, devices, load_device="cuda:0"):
        model = Mock()
        model.parameters.return_value = [types.SimpleNamespace(device=x) for x in devices]
        model.buffers.return_value = []
        return types.SimpleNamespace(model=model, load_device=load_device, offload_device="cpu")

    def test_mode(self):
        self.module.require_mode()
        for field, value in (("highvram", False), ("gpu_only", True)):
            setattr(self.args, field, value)
            with self.assertRaises(RuntimeError):
                self.module.require_mode()
            self.args.highvram, self.args.gpu_only = True, False
        self.torch.cuda.device_count.return_value = 2
        with self.assertRaises(RuntimeError):
            self.module.require_mode()

    def test_gpu_models_must_be_fully_on_gpu(self):
        with self.assertRaisesRegex(RuntimeError, "Refusing"):
            self.module.fully_load_gpu(self.patcher(["cuda:0"], load_device="cpu"))
        self.mm.load_models_gpu.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "not entirely"):
            self.module.fully_load_gpu(self.patcher(["cuda:0", "cpu"]))
        self.module.fully_load_gpu(self.patcher(["cuda:0"]))

    def test_encoder_must_be_fully_on_cpu(self):
        clip = types.SimpleNamespace(patcher=self.patcher(["cpu"], load_device="cpu"))
        self.nodes.CLIPLoader.return_value.load_clip.return_value = (clip,)
        self.assertEqual(self.module.QwenImageUCEncoderCPU().load("te.safetensors"), (clip,))
        self.nodes.CLIPLoader.return_value.load_clip.assert_called_with("te.safetensors", "qwen_image", device="cpu")
        clip.patcher.model.parameters.return_value.append(types.SimpleNamespace(device="cuda:0"))
        with self.assertRaisesRegex(RuntimeError, "not entirely on cpu"):
            self.module.QwenImageUCEncoderCPU().load("te.safetensors")

    def test_ui_offers_only_pinned_files(self):
        manifest = ROOT / "models.json"
        self.assertEqual(self.module.pinned_file("diffusion_models", manifest), ["qwen-image-2.1-UC-Q4_K_M.gguf"])
        self.assertEqual(self.module.pinned_file("text_encoders", manifest), ["qwen3vl_8b_int8_convrot.safetensors"])


class MetricsTests(unittest.TestCase):
    @staticmethod
    def fake_prometheus_client():
        module = types.ModuleType("prometheus_client")
        module.CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"

        class CollectorRegistry:
            def __init__(self):
                self.metrics = {}
                self.samples = {}

        class Labeled:
            def __init__(self, metric, labels):
                self.metric, self.labels = metric, tuple(labels)

            def inc(self, amount=1.0):
                key = (self.metric.name, self.labels)
                self.metric.registry.samples[key] = self.metric.registry.samples.get(key, 0.0) + amount

            def set(self, value):
                self.metric.registry.samples[(self.metric.name, self.labels)] = float(value)

            def observe(self, value):
                registry = self.metric.registry
                for suffix, add in (("_count", 1.0), ("_sum", float(value))):
                    key = (self.metric.name + suffix, self.labels)
                    registry.samples[key] = registry.samples.get(key, 0.0) + add

        class Metric:
            def __init__(self, name, documentation=None, labelnames=(), registry=None, **ignored):
                self.name, self.labelnames = name, tuple(labelnames)
                self.registry = registry or CollectorRegistry()
                self.registry.metrics[self.name] = self

            def labels(self, *values):
                return Labeled(self, values)

            def inc(self, amount=1.0):
                self.labels().inc(amount)

            def set(self, value):
                self.labels().set(value)

            def observe(self, value):
                self.labels().observe(value)

        class Counter(Metric):
            pass

        class Gauge(Metric):
            pass

        class Histogram(Metric):
            pass

        def generate_latest(registry):
            lines = []
            for (name, labels), value in sorted(registry.samples.items()):
                if name.endswith("_count"):
                    base = name[:-6]
                elif name.endswith("_sum"):
                    base = name[:-4]
                else:
                    base = name
                metric = registry.metrics.get(base)
                labels_part = ""
                if metric is not None and metric.labelnames:
                    labels_part = "{" + ",".join(f'{n}="{v}"' for n, v in zip(metric.labelnames, labels)) + "}"
                lines.append(f"{name}{labels_part} {value}")
            return ("\n".join(lines) + "\n").encode()

        module.CollectorRegistry = CollectorRegistry
        module.Counter = Counter
        module.Gauge = Gauge
        module.Histogram = Histogram
        module.generate_latest = generate_latest
        return module

    @staticmethod
    def value(text, prefix):
        for line in text.splitlines():
            if line.startswith(prefix):
                return float(line.rsplit(" ", 1)[1])
        return None

    def setUp(self):
        self.args = types.SimpleNamespace(highvram=True, gpu_only=False)
        self.torch = types.SimpleNamespace(device=lambda x: x, cuda=Mock())
        self.torch.cuda.device_count.return_value = 1
        self.torch.cuda.mem_get_info.side_effect = lambda i: (8 * 1024**3, 16 * 1024**3)
        self.torch.cuda.max_memory_reserved.side_effect = lambda i: 2 * 1024**3
        self.mm = types.ModuleType("comfy.model_management")
        self.mm.load_models_gpu = Mock()
        comfy = types.ModuleType("comfy")
        comfy.model_management = self.mm
        routes = types.SimpleNamespace(get=lambda path: lambda fn: fn)
        modules = {"torch": self.torch, "nodes": Mock(), "comfy": comfy, "comfy.model_management": self.mm,
                   "comfy.cli_args": types.SimpleNamespace(args=self.args),
                   "aiohttp": types.SimpleNamespace(web=Mock()),
                   "server": types.SimpleNamespace(PromptServer=types.SimpleNamespace(
                       instance=types.SimpleNamespace(routes=routes))),
                   "prometheus_client": self.fake_prometheus_client()}
        with patch.dict(sys.modules, modules):
            self.module = load("local_qwen_uc", ROOT / "local_nodes/__init__.py")
        self.history, self.running, self.pending = {}, [], []

    def queue(self):
        return types.SimpleNamespace(get_current_queue_volatile=lambda: (list(self.running), list(self.pending)),
                                     get_history=lambda: dict(self.history))

    def test_queue_and_gpu_gauges(self):
        self.pending = [(1, "p-1", {}, {}, [], None)]
        text = self.module.Metrics().scrape(self.queue()).decode()
        self.assertEqual(self.value(text, "qwen_image_uc_queue_running"), 0.0)
        self.assertEqual(self.value(text, "qwen_image_uc_queue_pending"), 1.0)
        self.assertEqual(self.value(text, "qwen_image_uc_ready"), 1.0)
        self.assertEqual(self.value(text, 'qwen_image_uc_cuda_free_bytes{device="0"}'), 8 * 1024**3)
        self.assertEqual(self.value(text, 'qwen_image_uc_cuda_reserved_peak_bytes{device="0"}'), 2 * 1024**3)

    def test_finished_prompts_are_counted_once(self):
        now = time.time()
        self.history = {
            "p-ok": {"prompt": (0, "p-ok", {}, {"create_time": int((now - 42) * 1000)}, []),
                     "outputs": {}, "status": {"status_str": "success", "completed": True, "messages": []}},
            "p-err": {"prompt": (1, "p-err", {}, {"create_time": int((now - 63) * 1000)}, []),
                      "outputs": {}, "status": {"status_str": "error", "completed": False, "messages": ["boom"]}},
        }
        metrics = self.module.Metrics()
        text = metrics.scrape(self.queue()).decode()
        self.assertEqual(self.value(text, 'qwen_image_uc_generations_total{result="success"}'), 1.0)
        self.assertEqual(self.value(text, 'qwen_image_uc_generations_total{result="error"}'), 1.0)
        self.assertEqual(self.value(text, "qwen_image_uc_generation_seconds_count"), 2.0)
        total = self.value(text, "qwen_image_uc_generation_seconds_sum")
        self.assertTrue(104.0 <= total <= 110.0, total)
        again = metrics.scrape(self.queue()).decode()
        self.assertEqual(self.value(again, 'qwen_image_uc_generations_total{result="success"}'), 1.0)
        self.assertEqual(self.value(again, "qwen_image_uc_generation_seconds_count"), 2.0)

    def test_duration_falls_back_to_first_sighting(self):
        self.running = [(0, "p-live", {}, {}, [], None)]
        metrics = self.module.Metrics()
        metrics.scrape(self.queue())
        self.history = {"p-live": {"prompt": (0, "p-live", {}, {}, []),
                                   "outputs": {}, "status": {"status_str": "success", "completed": True,
                                                             "messages": []}}}
        text = metrics.scrape(self.queue()).decode()
        self.assertEqual(self.value(text, "qwen_image_uc_generation_seconds_count"), 1.0)
        self.assertGreaterEqual(self.value(text, "qwen_image_uc_generation_seconds_sum"), 0.0)

    def test_ready_requires_expected_devices(self):
        self.torch.cuda.device_count.return_value = 2
        text = self.module.Metrics().scrape(self.queue()).decode()
        self.assertEqual(self.value(text, "qwen_image_uc_ready"), 0.0)


if __name__ == "__main__":
    unittest.main()
