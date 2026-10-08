import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import backend


class BackendTest(unittest.TestCase):
    def test_run_limits_combined_subprocess_output(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaisesRegex(
                    backend.VpnError, "produced too much output"):
                backend.run([sys.executable, "-c",
                             f"import sys; sys.{stream}.buffer.write(b'x' * (2 * 1024 * 1024))"],
                            timeout=5)

    def test_run_enforces_exact_combined_limit(self):
        script = ("import sys; sys.stdout.buffer.write(b'x' * 524288); "
                  "sys.stderr.buffer.write(b'y' * int(sys.argv[1]))")
        result = backend.run([sys.executable, "-c", script, "524288"], timeout=5)
        self.assertEqual(len(result.stdout) + len(result.stderr), backend.MAX_OUTPUT_BYTES)
        with self.assertRaisesRegex(backend.VpnError, "produced too much output"):
            backend.run([sys.executable, "-c", script, "524289"], timeout=5)

    def test_run_preserves_stdin_and_both_output_streams(self):
        result = backend.run(
            [sys.executable, "-c",
             "import sys; print(sys.stdin.readline().strip()); print('warning', file=sys.stderr)"],
            input="secret\n", timeout=5,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "secret\n", "warning\n"))

    def test_run_still_times_out_without_output(self):
        with self.assertRaisesRegex(backend.VpnError, "timed out"):
            backend.run([sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.1)

    def test_status_reads_gateway_from_vpn_data(self):
        vpn = Mock()
        vpn.get_data_item.return_value = "https://vpn.example.com"
        vpn.get_user_name.return_value = "name"
        connection = Mock()
        connection.get_setting_vpn.return_value = vpn
        client = Mock()
        client.get_connection_by_uuid.return_value = connection
        with patch.object(backend, "profile_uuid", return_value="test-uuid"), patch.object(
                backend.NM.Client, "new", return_value=client), patch.object(
                backend, "nmcli", return_value="test-uuid"):
            self.assertEqual(backend.state(), {
                "connected": True, "server": "https://vpn.example.com", "username": "name"
            })
        vpn.get_data_item.assert_called_with("gateway")

    def test_existing_profile_removes_agent_only_secret_flags(self):
        with patch.object(backend, "profile_uuid", return_value="test-uuid"), patch.object(
                backend, "nmcli") as nmcli:
            backend.ensure_profile("https://vpn.example.com", "name")
        self.assertEqual(nmcli.call_args.args[4:6],
                         ("vpn.data", "gateway=https://vpn.example.com,protocol=anyconnect"))

    def test_server_requires_https_without_userinfo(self):
        self.assertEqual(backend.server_url("vpn.example.com/path"), "https://vpn.example.com/path")
        for address in ("http://vpn.example.com", "https://user:pass@vpn.example.com",
                        "https://vpn.example.com:bad", "vpn.example.com\nbad",
                        "vpn.example.com/path,other=option"):
            with self.subTest(address=address), self.assertRaises(backend.VpnError):
                backend.server_url(address)

    def test_authenticate_parses_only_expected_fields(self):
        text = ("COOKIE='private-cookie'\nHOST='10.0.0.1'\n"
                "CONNECT_URL='https://vpn.example.com/+vpn'\n"
                "FINGERPRINT='sha256:abc'\nRESOLVE='vpn.example.com:10.0.0.1'\n")
        with patch.object(backend, "run",
                          return_value=subprocess.CompletedProcess([], 0, text, "")) as runner:
            values = backend.authenticate("https://vpn.example.com", "name", "private-password")
        self.assertEqual(values["COOKIE"], "private-cookie")
        self.assertEqual(values["CONNECT_URL"], "https://vpn.example.com/+vpn")
        self.assertNotIn("HOST", values)
        self.assertNotIn("private-password", runner.call_args.args[0])
        self.assertEqual(runner.call_args.kwargs["input"], "private-password\n")

    def test_activation_passes_secrets_only_to_nm_memory(self):
        values = {"COOKIE": "private-cookie", "CONNECT_URL": "https://vpn.example.com",
                  "FINGERPRINT": "sha256:abc"}
        setting = Mock()
        connection = Mock()
        connection.get_setting_vpn.return_value = setting
        connection.commit_changes.return_value = True
        client = Mock()
        client.get_connection_by_uuid.return_value = connection
        bus = Mock()
        with patch.object(backend.NM.Client, "new", return_value=client), patch.object(
                backend.Gio, "bus_get_sync", return_value=bus), patch.object(
                backend, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as runner:
            backend.activate("00000000-0000-0000-0000-000000000000", values)
        setting.add_secret.assert_any_call("cookie", "private-cookie")
        setting.add_data_item.assert_any_call("cookie-flags", "0")
        setting.add_data_item.assert_any_call("cookie-flags", "2")
        self.assertEqual(bus.call_sync.call_args.args[3], "ClearSecrets")
        self.assertEqual(connection.commit_changes.call_count, 2)
        self.assertEqual(connection.commit_changes.call_args.args, (False, None))
        self.assertNotIn("private-cookie", " ".join(runner.call_args.args[0]))

    def test_activation_failure_restores_agent_flags_and_hides_the_cookie(self):
        values = {"COOKIE": "private-cookie", "CONNECT_URL": "https://vpn.example.com",
                  "FINGERPRINT": "sha256:abc"}
        setting = Mock()
        connection = Mock()
        connection.get_setting_vpn.return_value = setting
        connection.commit_changes.return_value = True
        client = Mock()
        client.get_connection_by_uuid.return_value = connection
        bus = Mock()
        failure = subprocess.CompletedProcess(
            [], 4, "", "Error: Connection activation failed: No agents were available for this request.",
        )
        with patch.object(backend.NM.Client, "new", return_value=client), patch.object(
                backend.Gio, "bus_get_sync", return_value=bus), patch.object(
                backend, "run", return_value=failure):
            with self.assertRaisesRegex(backend.VpnError, "No agents were available") as error:
                backend.activate("00000000-0000-0000-0000-000000000000", values)
        self.assertNotIn("private-cookie", str(error.exception))
        setting.add_data_item.assert_any_call("cookie-flags", "2")
        self.assertEqual(bus.call_sync.call_args.args[3], "ClearSecrets")

    def test_failed_authentication_never_exposes_cookie(self):
        with patch.object(backend, "run",
                          return_value=subprocess.CompletedProcess([], 2, "", "private-password")):
            with self.assertRaisesRegex(backend.VpnError, "OpenConnect authentication failed") as error:
                backend.authenticate("https://vpn.example.com", "name", "private-password")
            self.assertNotIn("private-password", str(error.exception))

    def test_authentication_error_classifies_without_exposing_server_output(self):
        cases = (
            ("Server certificate verify failed: vpn.example.com", "certificate"),
            ("Failed to connect to vpn.example.com: Connection refused", "Could not reach"),
            ("Failed to resolve host vpn.example.com", "name could not be resolved"),
            ("Authentication failed: wrong password for user name", "rejected the credentials"),
            ("Login failed.\nUser input required in non-interactive mode\n", "rejected the credentials"),
            ("Authentication requires form entry: token for user name", "additional sign-in step"),
        )
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                message = backend.authentication_error(stderr)
                self.assertIn(expected, message)
                self.assertNotIn("vpn.example.com", message)
                self.assertNotIn("user name", message)

    def test_connect_reads_one_line_without_waiting_for_eof(self):
        request = {"server": "vpn.example.com", "username": "name", "password": "secret"}
        stdin = io.StringIO(json.dumps(request) + "\nextra input")
        with patch.object(sys, "argv", ["backend.py", "connect"]), patch.object(
                sys, "stdin", stdin), patch.object(
                backend, "ensure_profile", return_value="uuid"), patch.object(
                backend, "authenticate", return_value={"COOKIE": "cookie"}), patch.object(
                backend, "activate"), patch.object(
                backend, "state", return_value={"connected": True}) as state:
            self.assertEqual(backend.main(), state.return_value)
        self.assertEqual(stdin.read(), "extra input")

    def test_cancel_stops_child(self):
        child = Mock()
        child.poll.return_value = None
        with patch.object(backend, "active_child", child):
            with self.assertRaisesRegex(backend.VpnCancelled, "cancelled"):
                backend.cancel(None, None)
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=2)

    def test_load_known_keeps_complete_profiles_only(self):
        text = json.dumps([
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
            {"name": "bad\nname", "server": "vpn.example.com", "username": "user"},
            {"server": "missing-name.example.com", "username": "user"},
            "not-an-object",
        ])
        with patch("builtins.open", return_value=io.StringIO(text)):
            profiles = backend.load_known("/tmp/does-not-matter.json")
        self.assertEqual(profiles, (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
        ))

    def test_status_lists_saved_flag_without_reading_the_secret(self):
        sample = (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
            {"name": "office-vpn-b", "label": "Other office", "server": "other.example.com", "username": "user"},
        )

        def uuid_for(name=backend.PROFILE):
            return {"office-vpn": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "office-vpn-b": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}.get(name, "")

        with patch.object(backend, "KNOWN", sample), patch.object(
                backend, "profile_uuid", side_effect=uuid_for), patch.object(
                backend, "password_saved", side_effect=lambda name: name == "office-vpn"), patch.object(
                backend, "lookup_password") as lookup, patch.object(
                backend, "nmcli", return_value="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), patch.object(
                backend, "state", return_value={"connected": False, "server": "", "username": ""}):
            report = backend.status_report()
        lookup.assert_not_called()
        self.assertTrue(report["connected"])
        self.assertEqual(report["profile"], "office-vpn")
        self.assertTrue(report["saved"])
        self.assertEqual([item["saved"] for item in report["profiles"]], [True, False])
        self.assertNotIn("password", json.dumps(report))

    def test_blank_password_uses_the_keyring_and_does_not_rewrite_it(self):
        sample = (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
        )
        request = {"server": "vpn.example.com", "username": "user", "password": "",
                   "profile": "office-vpn", "remember": True, "share": True}
        with patch.object(backend, "KNOWN", sample), patch.object(
                sys, "argv", ["backend.py", "connect"]), patch.object(
                sys, "stdin", io.StringIO(json.dumps(request) + "\n")), patch.object(
                backend, "lookup_password", return_value="secret") as lookup, patch.object(
                backend, "ensure_named_profile", return_value="uuid") as named, patch.object(
                backend, "ensure_profile") as generic, patch.object(
                backend, "authenticate", return_value={"COOKIE": "cookie"}) as authenticate, patch.object(
                backend, "store_password") as store, patch.object(
                backend, "activate"), patch.object(
                backend, "status_report", return_value={"connected": True, "saved": True}):
            self.assertEqual(backend.main()["connected"], True)
        lookup.assert_called_once_with("office-vpn")
        named.assert_called_once()
        generic.assert_not_called()
        self.assertEqual(authenticate.call_args.args[2], "secret")
        store.assert_not_called()

    def test_typed_password_is_stored_only_after_authentication(self):
        sample = (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
            {"name": "office-vpn-b", "label": "Other office", "server": "other.example.com", "username": "user"},
        )
        request = {"server": "vpn.example.com", "username": "user", "password": "secret",
                   "profile": "office-vpn", "remember": True, "share": True}
        order = []

        def authenticate(*_args):
            order.append("auth")
            return {"COOKIE": "cookie"}

        def store(profile, password):
            order.append(profile)
            self.assertEqual(password, "secret")

        with patch.object(backend, "KNOWN", sample), patch.object(
                sys, "argv", ["backend.py", "connect"]), patch.object(
                sys, "stdin", io.StringIO(json.dumps(request) + "\n")), patch.object(
                backend, "ensure_named_profile", return_value="uuid"), patch.object(
                backend, "authenticate", side_effect=authenticate), patch.object(
                backend, "store_password", side_effect=store), patch.object(
                backend, "activate"), patch.object(
                backend, "status_report", return_value={"connected": True}):
            backend.main()
        self.assertEqual(order, ["auth", "office-vpn", "office-vpn-b"])

    def test_failed_authentication_does_not_store_password(self):
        sample = (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
        )
        request = {"server": "vpn.example.com", "username": "user", "password": "secret",
                   "profile": "office-vpn", "remember": True, "share": True}
        with patch.object(backend, "KNOWN", sample), patch.object(
                sys, "argv", ["backend.py", "connect"]), patch.object(
                sys, "stdin", io.StringIO(json.dumps(request) + "\n")), patch.object(
                backend, "ensure_named_profile", return_value="uuid"), patch.object(
                backend, "authenticate", side_effect=backend.VpnError("VPN rejected the credentials")), patch.object(
                backend, "store_password") as store, patch.object(
                backend, "activate") as activate:
            with self.assertRaisesRegex(backend.VpnError, "rejected the credentials"):
                backend.main()
        store.assert_not_called()
        activate.assert_not_called()

    def test_remember_off_leaves_the_keyring_unchanged(self):
        sample = (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
        )
        request = {"server": "vpn.example.com", "username": "user", "password": "secret",
                   "profile": "office-vpn", "remember": False, "share": True}
        with patch.object(backend, "KNOWN", sample), patch.object(
                sys, "argv", ["backend.py", "connect"]), patch.object(
                sys, "stdin", io.StringIO(json.dumps(request) + "\n")), patch.object(
                backend, "ensure_named_profile", return_value="uuid"), patch.object(
                backend, "authenticate", return_value={"COOKIE": "cookie"}), patch.object(
                backend, "store_password") as store, patch.object(
                backend, "activate"), patch.object(
                backend, "status_report", return_value={"connected": True}):
            backend.main()
        store.assert_not_called()

    def test_named_profile_keeps_gateway_host_and_rejects_a_mismatch(self):
        sample = (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
        )
        vpn = Mock()
        vpn.get_data_item.return_value = "anyconnect"
        connection = Mock()
        connection.get_setting_vpn.return_value = vpn
        connection.commit_changes.return_value = True
        client = Mock()
        client.get_connection_by_uuid.return_value = connection
        with patch.object(backend, "KNOWN", sample), patch.object(
                backend, "profile_uuid", return_value="uuid-1"), patch.object(
                backend.NM.Client, "new", return_value=client), patch.object(
                backend, "nmcli") as nmcli:
            self.assertEqual(
                backend.ensure_named_profile("office-vpn", "https://vpn.example.com", "user"),
                "uuid-1",
            )
            with self.assertRaisesRegex(backend.VpnError, "does not match"):
                backend.ensure_named_profile("office-vpn", "https://evil.example.com", "user")
        vpn.add_data_item.assert_called_once_with("gateway", "vpn.example.com")
        vpn.set_property.assert_called_once_with("user-name", "user")
        self.assertEqual(connection.commit_changes.call_count, 1)
        nmcli.assert_called_once_with(
            "connection", "modify", "uuid", "uuid-1",
            "connection.autoconnect", "no",
            "ipv4.never-default", "no", "ipv6.never-default", "no",
        )

    def test_disconnect_stops_an_active_known_profile(self):
        sample = (
            {"name": "office-vpn", "label": "Office", "server": "vpn.example.com", "username": "user"},
        )
        calls = []

        def uuid_for(name=backend.PROFILE):
            return "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" if name == "office-vpn" else ""

        def nmcli(*args):
            calls.append(args)
            if "--active" in args:
                return "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            return ""

        with patch.object(backend, "KNOWN", sample), patch.object(
                backend, "profile_uuid", side_effect=uuid_for), patch.object(
                backend, "nmcli", side_effect=nmcli):
            backend.disconnect_vpn()
        self.assertIn(
            ("connection", "down", "uuid", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            calls,
        )

    def test_unknown_profile_is_rejected_before_authentication(self):
        request = {"server": "vpn.example.com", "username": "user", "password": "secret",
                   "profile": "Nope"}
        with patch.object(sys, "argv", ["backend.py", "connect"]), patch.object(
                sys, "stdin", io.StringIO(json.dumps(request) + "\n")), patch.object(
                backend, "authenticate") as authenticate, patch.object(
                backend, "ensure_profile") as generic:
            with self.assertRaisesRegex(backend.VpnError, "Unknown VPN profile"):
                backend.main()
        authenticate.assert_not_called()
        generic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
