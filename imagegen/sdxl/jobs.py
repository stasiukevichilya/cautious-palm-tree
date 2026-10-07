import copy
import json
import logging
import secrets
import threading
import time
import uuid
from collections import deque

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from backend import Cancelled

log = logging.getLogger("sdxl")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Unavailable(Exception):
    pass


class QueueFull(Exception):
    pass


class Jobs:
    def __init__(self, settings, backend):
        self.settings, self.backend = settings, backend
        self.directory = settings.output_path
        self.directory.mkdir(parents=True, exist_ok=True)
        self.condition = threading.Condition(threading.RLock())
        self.pending, self.records, self.cancels = deque(), {}, {}
        self.ready, self.error, self.stopping, self.active = False, None, False, None
        self.registry = CollectorRegistry()
        self.requests = Counter("sdxl_requests_total", "Accepted jobs", registry=self.registry)
        self.images = Counter("sdxl_images_total", "Completed images", registry=self.registry)
        self.errors = Counter("sdxl_errors_total", "Failed jobs", registry=self.registry)
        self.ooms = Counter("sdxl_oom_total", "CUDA out of memory", registry=self.registry)
        self.duration = Histogram("sdxl_generation_seconds", "Generation duration",
                                  buckets=(5, 10, 20, 40, 80, 160, 320, 640), registry=self.registry)
        self.ready_metric = Gauge("sdxl_ready", "Pipeline ready", registry=self.registry)
        self.queued_metric = Gauge("sdxl_queue_depth", "Waiting jobs", registry=self.registry)
        self.active_metric = Gauge("sdxl_active", "Active jobs", registry=self.registry)
        self.peak = Gauge("sdxl_cuda_peak_bytes", "Last generation peak", ["gpu", "kind"], registry=self.registry)
        self._restore()
        self.thread = threading.Thread(target=self._run, name="sdxl-worker", daemon=True)

    def _save(self, record):
        target = self.directory / f"{record['id']}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(target)

    def _restore(self):
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if str(uuid.UUID(record["id"])) != path.stem:
                    continue
                if record["status"] not in TERMINAL:
                    record.update(status="interrupted", error="Service restarted", finished_at=time.time())
                    self._save(record)
                self.records[record["id"]] = record
            except (ValueError, KeyError, TypeError):
                log.warning("Ignoring invalid job metadata: %s", path.name)
        self._prune()

    def _prune(self):
        finished = sorted((r for r in self.records.values() if r["status"] in TERMINAL),
                          key=lambda r: r["created_at"], reverse=True)
        threshold = time.time() - self.settings.retention_hours * 3600
        for index, record in enumerate(finished):
            if index >= self.settings.max_results or record["created_at"] < threshold:
                for suffix in (".json", ".png"):
                    (self.directory / (record["id"] + suffix)).unlink(missing_ok=True)
                self.records.pop(record["id"])

    def start(self):
        self.thread.start()

    def stop(self):
        with self.condition:
            self.stopping, self.ready = True, False
            self.ready_metric.set(0)
            for cancel in self.cancels.values():
                cancel.set()
            self.condition.notify_all()
        self.thread.join(timeout=60)

    def submit(self, parameters):
        with self.condition:
            if not self.ready or self.stopping:
                raise Unavailable(self.error or "Model is loading")
            if len(self.pending) >= self.settings.queue_size:
                raise QueueFull()
            self._prune()
            parameters = dict(parameters)
            if parameters["seed"] is None:
                parameters["seed"] = secrets.randbits(32)
            identifier = str(uuid.uuid4())
            record = {"id": identifier, "status": "queued", "created_at": time.time(),
                      "parameters": parameters, "progress": 0, "error": None, "metadata": None}
            self._save(record)
            self.records[identifier] = record
            self.cancels[identifier] = threading.Event()
            self.pending.append(identifier)
            self.requests.inc()
            self.queued_metric.set(len(self.pending))
            self.condition.notify()
            return copy.deepcopy(record)

    def get(self, identifier):
        with self.condition:
            return copy.deepcopy(self.records[identifier])

    def history(self):
        with self.condition:
            self._prune()
            return copy.deepcopy(sorted(self.records.values(), key=lambda r: r["created_at"], reverse=True))

    def cancel(self, identifier):
        with self.condition:
            record = self.records[identifier]
            if record["status"] not in TERMINAL:
                self.cancels[identifier].set()
                if record["status"] == "queued":
                    self.pending.remove(identifier)
                    record.update(status="cancelled", finished_at=time.time())
                    self._save(record)
                    self.cancels.pop(identifier)
                    self.queued_metric.set(len(self.pending))
                else:
                    record["status"] = "cancelling"
            return copy.deepcopy(record)

    def _run(self):
        try:
            self.backend.load()
            with self.condition:
                self.ready = not self.stopping
                self.ready_metric.set(int(self.ready))
            while not self.stopping:
                with self.condition:
                    self.condition.wait_for(lambda: self.pending or self.stopping, timeout=60)
                    self._prune()
                    if self.stopping:
                        break
                    if not self.pending:
                        continue
                    identifier = self.pending.popleft()
                    record = self.records[identifier]
                    record.update(status="running", started_at=time.time())
                    self.active = identifier
                    self._save(record)
                    self.queued_metric.set(len(self.pending))
                    self.active_metric.set(1)
                self._execute(record)
        except Exception as error:
            log.error("Worker stopped: %s", type(error).__name__)
            with self.condition:
                self.ready = False
                self.error = "Pipeline failed; inspect container logs and restart"
                self.ready_metric.set(0)
                for identifier in list(self.pending):
                    record = self.records[identifier]
                    record.update(status="failed", error=self.error, finished_at=time.time())
                    self._save(record)
                    self.cancels.pop(identifier, None)
                self.pending.clear()
                self.queued_metric.set(0)
            # Startup errors do not contain user prompts and are useful diagnostics.
            if self.active is None:
                log.error("Startup/recovery failure: %s", error)

    def _execute(self, record):
        identifier = record["id"]

        def progress(step):
            with self.condition:
                record["progress"] = step

        recover = False
        try:
            image, metadata = self.backend.generate(record["parameters"], self.cancels[identifier], progress)
            with self.condition:
                if self.cancels[identifier].is_set():
                    raise Cancelled()
                temporary = self.directory / f"{identifier}.png.tmp"
                image.save(temporary, format="PNG")
                temporary.replace(self.directory / f"{identifier}.png")
                record.update(status="completed", metadata=metadata)
                self.images.inc()
                self.duration.observe(metadata["seconds"])
                for gpu in metadata.get("memory", []):
                    for kind in ("allocated", "reserved"):
                        self.peak.labels(str(gpu["index"]), kind).set(gpu[f"{kind}_peak_bytes"])
        except Cancelled:
            record["status"] = "cancelled"
            recover = True
        except ValueError as error:
            record.update(status="failed", error=str(error))
            self.errors.inc()
        except Exception as error:
            oom = self.backend.is_oom(error)
            record.update(status="failed", error="GPU out of memory" if oom else "Generation failed")
            self.errors.inc()
            if oom:
                self.ooms.inc()
            log.error("Generation failed: %s", type(error).__name__)
            recover = True
        finally:
            with self.condition:
                record["finished_at"] = time.time()
                self._save(record)
                self.cancels.pop(identifier, None)
                self.active_metric.set(0)
                self.active = None
        if recover and not self.stopping:
            with self.condition:
                self.ready = False
                self.ready_metric.set(0)
            self.backend.recover()
            with self.condition:
                self.ready = True
                self.ready_metric.set(1)

