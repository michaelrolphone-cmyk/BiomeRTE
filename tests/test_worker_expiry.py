import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from biomerte.store import Fault
from biomerte.worker import Worker
from biomerte.worker_expiry import expire


class SharedResourceExpiry(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.inventory = {"actions": [], "activations": [], "fences": []}
        remote = SimpleNamespace(site="site", realm="site", node="vision",
                                 call=Mock(return_value=self.inventory))
        self.offers = [{"contract": contract, "resource": "camera", "argv": ["/unused", contract]}
                       for contract in ("vision.status:1", "vision.frame:1")]
        self.worker = Worker(str(self.directory / "worker.sqlite"), remote, self.offers)
        self.addCleanup(self.worker.db.close)

    def acknowledged(self, identifier, guard="!slot:camera:1"):
        self.worker.db.execute("INSERT INTO actions VALUES(?,?,?,?,?)",
                               (identifier, "signature", "succeeded", "{}", guard))
        self.worker.db.execute("INSERT INTO result_acknowledged VALUES(?)", (identifier,))

    def retained(self):
        return {row[0] for row in self.worker.db.execute("SELECT id FROM actions")}

    def test_provider_poll_expires_compacted_record_with_no_cleanup_and_keeps_debug_window(self):
        self.acknowledged("eligible")
        self.acknowledged("debug")
        self.worker.db.execute("INSERT INTO result_debug(id,state,result,bytes) VALUES(?,?,?,?)",
                               ("debug", "succeeded", "{}", 2))
        with patch("biomerte.worker_expiry.subprocess.Popen") as run:
            self.worker.once()
            run.assert_not_called()
        self.assertEqual(self.retained(), {"debug"})

    def test_identical_cleanup_commands_execute_once_with_the_expiry_request(self):
        self.acknowledged("one", guard="camera")
        receipt = self.directory / "calls"
        argv = [sys.executable, "-c",
                "import sys; open(sys.argv[1],'ab').write(sys.stdin.buffer.read()+b'\\n'); "
                "print('{\"expired\": true}')", str(receipt)]
        for offer in self.offers:
            offer["acknowledge_argv"] = list(argv)
        self.assertEqual(expire(self.worker)["deleted"], ["one"])
        self.assertEqual([json.loads(line) for line in receipt.read_text().splitlines()],
                         [{"operation": "one", "expire": True}])
        self.assertFalse(self.retained())

    def test_different_cleanup_commands_and_unmatched_resources_retain_records(self):
        self.acknowledged("ambiguous")
        argv = ["/bin/echo", "first"]
        self.offers[1]["acknowledge_argv"] = argv
        with patch("biomerte.worker_expiry.subprocess.Popen") as run:
            for first in (None, ["/bin/echo", "second"]):
                with self.subTest(first=first):
                    self.offers[0]["acknowledge_argv"] = first
                    with self.assertRaisesRegex(Fault, "Ambiguous adapter expiry route"):
                        expire(self.worker)
                    self.assertEqual(self.retained(), {"ambiguous"})
            for offer in self.offers:
                offer["resource"] = "different"
            with self.assertRaisesRegex(Fault, "Ambiguous adapter expiry route"):
                expire(self.worker)
            self.assertEqual(self.retained(), {"ambiguous"})
            run.assert_not_called()

    def test_malformed_shared_cleanup_cannot_expire_a_record(self):
        self.acknowledged("malformed")
        for offer in self.offers:
            offer["acknowledge_argv"] = []
        with self.assertRaisesRegex(Fault, "Invalid adapter expiry argv"):
            expire(self.worker)
        self.assertEqual(self.retained(), {"malformed"})
