from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gridzilla.runtime import EndpointTransport


def token_with_expiry(expiry: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"e30.{payload}.signature"


class EppCredentialRefreshTests(unittest.TestCase):
    def test_expiring_handoff_service_account_token_is_rotated_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            credential = Path(temporary) / "epp.token"
            credential.write_text(token_with_expiry(int(time.time()) - 1) + "\n", encoding="utf-8")
            os.chmod(credential, 0o600)
            refreshed = token_with_expiry(int(time.time()) + 3600)
            site = {"name": "west", "namespace": "grid-system",
                    "_cluster": {"kubeconfig": "/run-owned/west.kubeconfig", "context": "west"}}
            endpoint = {"namespace": "grid-system", "auth": {
                "type": "serviceAccountToken", "serviceAccount": "grid-operator-metrics-scraper"}}
            transport = EndpointTransport({})
            command = subprocess.CompletedProcess(["kubectl"], 0, (refreshed + "\n").encode(), b"")
            with patch("gridzilla.runtime.run_command", return_value=command) as run:
                observed = transport._bearer_token(site, endpoint, str(credential))
            self.assertEqual(observed, refreshed, "request should use the refreshed ServiceAccount token")
            self.assertEqual(credential.read_text(encoding="utf-8").strip(), refreshed,
                             "rotated credential should replace the expired handoff token")
            self.assertEqual(credential.stat().st_mode & 0o777, 0o600,
                             "rotated credential must remain private")
            argv = run.call_args.args[0]
            self.assertEqual(argv[-4:], ["create", "token", "grid-operator-metrics-scraper", "--duration=24h"])
            self.assertNotIn(refreshed, argv, "the token value must not be passed in command arguments")

    def test_non_service_account_bearer_is_never_rotated(self):
        with tempfile.TemporaryDirectory() as temporary:
            credential = Path(temporary) / "generic.token"
            existing = token_with_expiry(int(time.time()) - 1)
            credential.write_text(existing, encoding="utf-8")
            transport = EndpointTransport({})
            with patch("gridzilla.runtime.run_command") as run:
                observed = transport._bearer_token({"name": "endpoint"}, {"auth": {"type": "static"}},
                                                   str(credential))
            self.assertEqual(observed, existing)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
