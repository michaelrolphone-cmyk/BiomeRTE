"""Regress the pinned pySLAM logger factory without importing native SLAM engines."""
import ast
import concurrent.futures
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid

PROVIDER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROVIDER))
from setup_slam import patch_logging_file_handlers

FIXTURE = Path(__file__).with_name("fixtures") / "pyslam_logging.py"
SOURCE_PATH = Path(os.environ["PYSLAM_LOGGING_SOURCE"]) if os.environ.get("PYSLAM_LOGGING_SOURCE") else FIXTURE
UPSTREAM_SOURCE = SOURCE_PATH.read_text()


def factory(source):
    # Execute the real pinned Logging class only; unrelated cv2/torch/download
    # imports in the full upstream utility module are irrelevant to file ownership.
    tree = ast.parse(source)
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Logging")
    scope = {"logging": logging, "os": os, "threading": threading}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE_PATH), "exec"), scope)
    return scope["Logging"]


class FileLoggerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.prefix = "pyslam-regression-" + uuid.uuid4().hex
        self.names = []
        self.addCleanup(self.close_loggers)
        self.logging = factory(patch_logging_file_handlers(UPSTREAM_SOURCE))

    def name(self, suffix):
        name = self.prefix + "." + suffix
        if name not in self.names:
            self.names.append(name)
            logging.getLogger(name).propagate = False
        return name

    def close_loggers(self):
        for name in self.names:
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logging.Logger.manager.loggerDict.pop(name, None)

    def configure(self, suffix, path=None, **kwargs):
        return self.logging.setup_file_logger(
            self.name(suffix), path or self.root / (suffix + ".log"),
            formatter=kwargs.pop("formatter", logging.Formatter("%(message)s")), **kwargs
        )

    def test_unpatched_factory_reproduces_retained_file_descriptors(self):
        original = factory(UPSTREAM_SOURCE)
        name = self.name("unpatched")
        for unused in range(12):
            logger = original.setup_file_logger(name, self.root / "unpatched.log", mode="a")
            # Dropping a runtime's logger reference does not remove the global
            # logging manager's handlers, exactly the reconstruction boundary.
            del logger
        handlers = logging.getLogger(name).handlers
        self.assertEqual(12, len(handlers))
        self.assertTrue(all(handler.stream is not None for handler in handlers))

    def test_repeated_runtime_logger_lifetimes_keep_four_sinks_and_single_writes(self):
        streams = {}
        for cycle in range(80):
            runtime_loggers = [
                self.configure(suffix, mode="a")
                for suffix in ("gba", "local_mapping", "loop_closing", "kf_info")
            ]
            for suffix, logger in zip(("gba", "local_mapping", "loop_closing", "kf_info"), runtime_loggers):
                self.assertEqual(1, len(logger.handlers))
                stream = logger.handlers[0].stream
                if suffix in streams:
                    self.assertIs(streams[suffix], stream)
                streams[suffix] = stream
                logger.info("cycle-%d", cycle)
            runtime_loggers.clear()
        descriptors = {stream.fileno() for stream in streams.values()}
        self.assertEqual(4, len(descriptors))
        for descriptor in descriptors:
            os.fstat(descriptor)
        expected = ["cycle-" + str(cycle) for cycle in range(80)]
        for suffix in streams:
            self.assertEqual(expected, (self.root / (suffix + ".log")).read_text().splitlines())
        self.close_loggers()
        self.assertTrue(all(stream.closed for stream in streams.values()))

    def test_one_runtime_releasing_its_reference_preserves_the_surviving_owner(self):
        departing = self.configure("shared", mode="a")
        surviving = self.configure("shared", mode="a")
        stream = surviving.handlers[0].stream
        departing.info("both-live")
        del departing
        surviving.info("survivor")
        self.assertFalse(stream.closed)
        self.assertEqual(1, len(surviving.handlers))
        self.assertEqual(["both-live", "survivor"], (self.root / "shared.log").read_text().splitlines())

    def test_default_write_mode_does_not_retruncate_on_reconstruction(self):
        logger = self.configure("default")
        stream = logger.handlers[0].stream
        logger.info("before")
        same = self.configure("default", level=logging.DEBUG, formatter=logging.Formatter("new:%(message)s"))
        self.assertIs(logger, same)
        self.assertIs(stream, same.handlers[0].stream)
        same.debug("after")
        self.assertEqual(["before", "new:after"], (self.root / "default.log").read_text().splitlines())

    def test_reconfigure_closes_previous_owned_sink_and_preserves_other_handlers(self):
        logger = self.configure("redirect", mode="a")
        previous = logger.handlers[0]
        previous_stream = previous.stream
        external = logging.FileHandler(self.root / "external.log")
        external.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(external)
        logger.info("first")
        logger = self.configure("redirect", self.root / "replacement.log", mode="a")
        self.assertTrue(previous_stream.closed)
        self.assertIsNone(previous.stream)
        self.assertIn(external, logger.handlers)
        self.assertEqual(2, len(logger.handlers))
        logger.info("second")
        self.assertEqual(["first"], (self.root / "redirect.log").read_text().splitlines())
        self.assertEqual(["second"], (self.root / "replacement.log").read_text().splitlines())
        self.assertEqual(["first", "second"], (self.root / "external.log").read_text().splitlines())

    def test_failed_reconfiguration_keeps_existing_sink_working(self):
        logger = self.configure("failure", mode="a")
        previous = logger.handlers[0]
        with self.assertRaises(OSError):
            self.configure("failure", self.root, mode="a")
        self.assertEqual([previous], logger.handlers)
        self.assertIsNotNone(previous.stream)
        logger.info("still-working")
        self.assertEqual("still-working\n", (self.root / "failure.log").read_text())

    def test_closed_sink_is_replaced_and_explicit_mode_change_is_honored(self):
        logger = self.configure("closed", mode="a")
        old = logger.handlers[0]
        logger.info("before")
        old.close()
        logger = self.configure("closed", mode="a")
        self.assertNotIn(old, logger.handlers)
        self.assertEqual(1, len(logger.handlers))
        logger.info("after")
        active_stream = logger.handlers[0].stream
        logger = self.configure("closed", mode="w")
        self.assertTrue(active_stream.closed)
        logger.info("reconfigured")
        self.assertEqual("reconfigured\n", (self.root / "closed.log").read_text())

    def test_concurrent_construction_does_not_duplicate_writes_or_handlers(self):
        name = self.name("concurrent")
        barrier = threading.Barrier(16)
        def construct(index):
            barrier.wait(timeout=10)
            logger = self.logging.setup_file_logger(
                name, self.root / "concurrent.log", mode="a",
                formatter=logging.Formatter("%(message)s")
            )
            logger.info("worker-%d", index)
            return logger
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            loggers = list(pool.map(construct, range(16)))
        self.assertEqual(1, len({id(logger) for logger in loggers}))
        self.assertEqual(1, len(loggers[0].handlers))
        self.assertEqual(
            sorted("worker-" + str(index) for index in range(16)),
            sorted((self.root / "concurrent.log").read_text().splitlines())
        )

    @unittest.skipUnless(os.environ.get("PYSLAM_LOGGING_SOURCE"), "full pinned source supplied by native integration run")
    def test_full_pinned_source_hashes_match_the_installation_manifest(self):
        manifest = json.loads((PROVIDER / "pyslam-patches.json").read_text())
        entry = next(row for row in manifest["patches"] if row["path"] == "pyslam/utilities/logging.py")
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        self.assertEqual(entry["original_sha256"], digest(UPSTREAM_SOURCE))
        self.assertEqual(entry["patched_sha256"], digest(patch_logging_file_handlers(UPSTREAM_SOURCE)))


if __name__ == "__main__":
    unittest.main()
