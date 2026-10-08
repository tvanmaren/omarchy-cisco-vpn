#!/usr/bin/env python3
"""Local Cisco VPN bridge: OpenConnect authentication, NetworkManager tunnel."""

import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

import gi

gi.require_version("NM", "1.0")
from gi.repository import Gio, GLib, NM


PROFILE = "Omarchy Cisco VPN"
SERVICE = "org.freedesktop.NetworkManager.openconnect"
UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
MAX_OUTPUT_BYTES = 1024 * 1024
PROFILES_PATH = os.path.expanduser("~/.config/omarchy/cisco-vpn-profiles.json")
SECRET_SCHEMA_NAME = "org.omarchy.CiscoVpn"


def load_known(path=None):
    try:
        with open(path or PROFILES_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()
    profiles = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        server = str(item.get("server", "")).strip()
        username = str(item.get("username", "")).strip()
        label = str(item.get("label", "")).strip() or name
        values = (name, server, username, label)
        if not name or not server or not username:
            continue
        if any(char in value for value in values for char in "\r\n"):
            continue
        profiles.append({
            "name": name,
            "label": label,
            "server": server,
            "username": username,
        })
    return tuple(profiles)


KNOWN = load_known()


def known_by_name():
    return {item["name"]: item for item in KNOWN}


class VpnError(Exception):
    pass


class VpnCancelled(VpnError):
    pass


active_child = None


def cancel(_signal, _frame):
    if active_child is not None and active_child.poll() is None:
        active_child.terminate()
        try:
            active_child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            active_child.kill()
            active_child.wait()
    raise VpnCancelled("Connection cancelled")


signal.signal(signal.SIGTERM, cancel)


def run(args, *, input=None, timeout=100):
    global active_child
    try:
        with subprocess.Popen(
            args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as child:
            active_child = child
            try:
                output = {child.stdout: [], child.stderr: []}
                total = 0
                deadline = time.monotonic() + timeout
                pending = memoryview(input.encode() if input is not None else b"")
                with selectors.DefaultSelector() as selector:
                    for stream in output:
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ)
                    if input is not None:
                        os.set_blocking(child.stdin.fileno(), False)
                        if pending:
                            selector.register(child.stdin, selectors.EVENT_WRITE)
                        else:
                            child.stdin.close()
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            child.kill()
                            child.wait()
                            raise VpnError(f"{args[0]} timed out")
                        for key, _ in selector.select(remaining):
                            stream = key.fileobj
                            if stream is child.stdin:
                                try:
                                    count = os.write(stream.fileno(), pending[:65536])
                                except BrokenPipeError:
                                    count = len(pending)
                                pending = pending[count:]
                                if not pending:
                                    selector.unregister(stream)
                                    stream.close()
                            else:
                                chunk = os.read(stream.fileno(), 65536)
                                if not chunk:
                                    selector.unregister(stream)
                                else:
                                    total += len(chunk)
                                    if total > MAX_OUTPUT_BYTES:
                                        child.kill()
                                        child.wait()
                                        raise VpnError(f"{args[0]} produced too much output")
                                    output[stream].append(chunk)
                try:
                    child.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as error:
                    child.kill()
                    child.wait()
                    raise VpnError(f"{args[0]} timed out") from error
                return subprocess.CompletedProcess(
                    args, child.returncode,
                    b"".join(output[child.stdout]).decode(errors="replace"),
                    b"".join(output[child.stderr]).decode(errors="replace"),
                )
            finally:
                active_child = None
    except OSError as error:
        raise VpnError(f"Could not start {args[0]}: {error.strerror}") from error


def nmcli(*args):
    result = run(["nmcli", *args])
    if result.returncode:
        raise VpnError(f"NetworkManager command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def profile_uuid(name=PROFILE):
    profiles = nmcli("-t", "--escape", "no", "-f", "UUID,TYPE,NAME", "connection", "show")
    matches = []
    for line in profiles.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2] == name:
            matches.append((parts[0], parts[1]))
    if not matches:
        return ""
    if len(matches) != 1 or matches[0][1] != "vpn" or not UUID.fullmatch(matches[0][0]):
        raise VpnError(f"Conflicting NetworkManager profile named {name}")
    uuid = matches[0][0]
    if nmcli("-g", "vpn.service-type", "connection", "show", "uuid", uuid) != SERVICE:
        raise VpnError(f"Conflicting NetworkManager profile named {name}")
    return uuid


def state():
    uuid = profile_uuid()
    if not uuid:
        return {"connected": False, "server": "", "username": ""}
    connection = NM.Client.new(None).get_connection_by_uuid(uuid)
    if connection is None or connection.get_setting_vpn() is None:
        raise VpnError("NetworkManager could not read the VPN profile")
    vpn = connection.get_setting_vpn()
    active = nmcli("-t", "-f", "UUID", "connection", "show", "--active").splitlines()
    return {
        "connected": uuid in active,
        "server": vpn.get_data_item("gateway") or "",
        "username": vpn.get_user_name() or "",
    }


def server_url(value):
    value = value.strip()
    if not value or "," in value or any(char.isspace() for char in value):
        raise VpnError("Enter a VPN server address without spaces or commas")
    address = value if "://" in value else "https://" + value
    parsed = urlsplit(address)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise VpnError("Enter a valid HTTPS VPN server address")
    try:
        parsed.port
    except ValueError as error:
        raise VpnError("Invalid VPN server port") from error
    return address


def ensure_profile(address, username):
    data = f"gateway={address},protocol=anyconnect"
    uuid = profile_uuid()
    if uuid:
        nmcli("connection", "modify", "uuid", uuid, "vpn.data", data, "vpn.user-name", username,
              "connection.autoconnect", "no",
              "ipv4.never-default", "no", "ipv6.never-default", "no")
    else:
        nmcli("connection", "add", "type", "vpn", "vpn-type", "openconnect",
              "ifname", "*", "con-name", PROFILE, "autoconnect", "no",
              "--", "vpn.data", data,
              "vpn.user-name", username,
              "ipv4.never-default", "no", "ipv6.never-default", "no")
        uuid = profile_uuid()
        if not uuid:
            raise VpnError("NetworkManager did not create the VPN profile")
    return uuid


def ensure_named_profile(name, address, username):
    known = known_by_name().get(name)
    if known is None:
        raise VpnError("Unknown VPN profile")
    parsed = urlsplit(address)
    known_url = known["server"] if "://" in known["server"] else "https://" + known["server"]
    known_host = urlsplit(known_url).hostname
    if not parsed.hostname or parsed.hostname != known_host:
        raise VpnError("That server does not match the selected VPN profile")
    uuid = profile_uuid(name)
    if not uuid:
        raise VpnError(f"NetworkManager profile {name} does not exist")
    client = NM.Client.new(None)
    connection = client.get_connection_by_uuid(uuid)
    if connection is None or connection.get_setting_vpn() is None:
        raise VpnError("NetworkManager could not read the VPN profile")
    vpn = connection.get_setting_vpn()
    vpn.add_data_item("gateway", known["server"] if "://" not in known["server"] else address)
    if not vpn.get_data_item("protocol"):
        vpn.add_data_item("protocol", "anyconnect")
    vpn.set_property("user-name", username)
    try:
        if not connection.commit_changes(False, None):
            raise VpnError("Could not update the VPN profile")
    except GLib.Error as error:
        raise VpnError("Could not update the VPN profile") from error
    nmcli("connection", "modify", "uuid", uuid,
          "connection.autoconnect", "no",
          "ipv4.never-default", "no", "ipv6.never-default", "no")
    return uuid


def secret_api():
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    schema = Secret.Schema.new(
        SECRET_SCHEMA_NAME,
        Secret.SchemaFlags.NONE,
        {"profile": Secret.SchemaAttributeType.STRING},
    )
    return Secret, schema


def password_saved(profile):
    """Report whether a keyring item exists. Does not unlock or read the secret."""
    try:
        Secret, schema = secret_api()
        service = Secret.Service.get_sync(
            Secret.ServiceFlags.OPEN_SESSION | Secret.ServiceFlags.LOAD_COLLECTIONS,
            None,
        )
        items = service.search_sync(schema, {"profile": profile}, Secret.SearchFlags.NONE, None)
        return bool(items)
    except Exception:
        return False


def lookup_password(profile):
    if not profile or any(char in profile for char in "\r\n"):
        raise VpnError("No saved password for this VPN; enter it once")
    try:
        Secret, schema = secret_api()
        value = Secret.password_lookup_sync(schema, {"profile": profile}, None)
    except VpnError:
        raise
    except Exception as error:
        raise VpnError("Could not read the saved VPN password from the keyring") from error
    if not value:
        raise VpnError("No saved password for this VPN; enter it once")
    return value


def store_password(profile, password):
    if not profile or not password or any(char in profile for char in "\r\n"):
        raise VpnError("Could not save the VPN password to the keyring")
    try:
        Secret, schema = secret_api()
        saved = Secret.password_store_sync(
            schema,
            {"profile": profile},
            Secret.COLLECTION_DEFAULT,
            f"Cisco VPN {profile}",
            password,
            None,
        )
    except VpnError:
        raise
    except Exception as error:
        raise VpnError("Could not save the VPN password to the keyring") from error
    if not saved:
        raise VpnError("Could not save the VPN password to the keyring")


def store_typed_password(profile_name, password, share):
    names = [item["name"] for item in KNOWN]
    if share and profile_name in names:
        targets = names
    elif profile_name:
        targets = [profile_name]
    else:
        targets = [PROFILE]
    for target in targets:
        store_password(target, password)


def active_uuids():
    return set(nmcli("-t", "-f", "UUID", "connection", "show", "--active").splitlines())


def status_report():
    active = active_uuids()
    profiles = []
    for item in KNOWN:
        uuid = profile_uuid(item["name"])
        profiles.append({
            "name": item["name"],
            "label": item["label"],
            "server": item["server"],
            "username": item["username"],
            "saved": password_saved(item["name"]),
            "connected": bool(uuid) and uuid in active,
            "present": bool(uuid),
        })
    connected_item = next((item for item in profiles if item["connected"]), None)
    legacy = state()
    if connected_item is None and legacy.get("connected"):
        return {
            "connected": True,
            "server": legacy.get("server", ""),
            "username": legacy.get("username", ""),
            "profile": PROFILE,
            "label": PROFILE,
            "saved": password_saved(PROFILE),
            "profiles": profiles,
        }
    selected = connected_item or (profiles[0] if profiles else None)
    if selected is None:
        return {
            "connected": False,
            "server": legacy.get("server", ""),
            "username": legacy.get("username", ""),
            "profile": "",
            "label": "",
            "saved": False,
            "profiles": [],
        }
    return {
        "connected": connected_item is not None,
        "server": selected["server"],
        "username": selected["username"],
        "profile": selected["name"],
        "label": selected["label"],
        "saved": selected["saved"],
        "profiles": profiles,
    }


def disconnect_vpn():
    active = active_uuids()
    seen = False
    for name in [item["name"] for item in KNOWN] + [PROFILE]:
        uuid = profile_uuid(name)
        if not uuid:
            continue
        seen = True
        if uuid in active:
            nmcli("connection", "down", "uuid", uuid)
    if not seen:
        raise VpnError("No Cisco VPN profile exists")


def authentication_error(stderr):
    details = stderr.lower()
    if any(term in details for term in ("certificate", "servercert", "issuer", "trust anchor")):
        return "VPN server certificate could not be verified; check with your IT team"
    if any(term in details for term in ("resolve", "getaddrinfo", "name or service not known")):
        return "VPN server name could not be resolved"
    if any(term in details for term in ("failed to connect", "connection refused", "network is unreachable")):
        return "Could not reach the VPN server; check the address and network"
    # A rejected password makes OpenConnect ask again, then exit because
    # --non-inter cannot answer that second prompt. The login failure is the cause.
    if any(term in details for term in ("login failed", "authentication failed", "invalid credentials", "incorrect password")):
        return "VPN rejected the credentials; check your username and password"
    if any(term in details for term in ("authgroup", "non-interactive", "form entry", "token", "challenge")):
        return "VPN requires an additional sign-in step (group or token) not supported by this widget"
    return "OpenConnect authentication failed; the gateway may require another login step"


def authenticate(address, username, password):
    result = run(
        ["openconnect", "--protocol=anyconnect", "--authenticate",
         "--non-inter", "--passwd-on-stdin", "--user", username, address],
        input=password + "\n", timeout=90,
    )
    if result.returncode:
        raise VpnError(authentication_error(result.stderr))

    values = {}
    for line in result.stdout.splitlines():
        key, separator, raw = line.partition("=")
        if not separator or key not in ("COOKIE", "CONNECT_URL", "FINGERPRINT", "RESOLVE"):
            continue
        try:
            parts = shlex.split(raw)
        except ValueError as error:
            raise VpnError("OpenConnect returned malformed authentication data") from error
        if len(parts) != 1 or "\n" in parts[0] or "\r" in parts[0]:
            raise VpnError("OpenConnect returned malformed authentication data")
        values[key] = parts[0]
    if not all(values.get(key) for key in ("COOKIE", "CONNECT_URL", "FINGERPRINT")):
        raise VpnError("OpenConnect did not return the gateway, cookie, and certificate fingerprint")
    return values


def secret_flags(vpn, value):
    # 0: this process may supply the secret. 2: ask a desktop secret agent.
    # The OpenConnect plugin refuses to start until cookie, gateway, and gwcert
    # are present, and flag 2 makes NetworkManager ask an agent this session lacks.
    for key in ("cookie-flags", "gateway-flags", "gwcert-flags", "resolve-flags"):
        vpn.add_data_item(key, value)


def activation_failure(result):
    lines = [line.strip() for line in f"{result.stderr}\n{result.stdout}".splitlines() if line.strip()]
    detail = lines[-1] if lines else f"NetworkManager exit code {result.returncode}"
    if len(detail) > 180 or "cookie" in detail.lower():
        detail = f"NetworkManager exit code {result.returncode}"
    return f"VPN activation failed ({detail})"


def activate(uuid, values):
    secrets = {
        "cookie": values["COOKIE"],
        "gateway": values["CONNECT_URL"],
        "gwcert": values["FINGERPRINT"],
    }
    if values.get("RESOLVE"):
        secrets["resolve"] = values["RESOLVE"]
    client = NM.Client.new(None)
    connection = client.get_connection_by_uuid(uuid)
    if connection is None:
        raise VpnError("NetworkManager could not find the VPN profile")
    vpn = connection.get_setting_vpn()
    if vpn is None:
        raise VpnError("NetworkManager VPN profile is invalid")
    secret_flags(vpn, "0")
    for key, value in secrets.items():
        vpn.add_secret(key, value)
    try:
        if not connection.commit_changes(False, None):
            raise VpnError("Could not pass VPN credentials to NetworkManager")
        result = run(["nmcli", "--wait", "90", "connection", "up", "uuid", uuid])
        if result.returncode:
            raise VpnError(activation_failure(result))
    except VpnCancelled:
        if uuid in nmcli("-t", "-f", "UUID", "connection", "show", "--active").splitlines():
            nmcli("connection", "down", "uuid", uuid)
        raise
    finally:
        restored = connection.get_setting_vpn()
        if restored is not None:
            secret_flags(restored, "2")
            try:
                connection.commit_changes(False, None)
            except GLib.Error:
                pass
        Gio.bus_get_sync(Gio.BusType.SYSTEM, None).call_sync(
            "org.freedesktop.NetworkManager", connection.get_path(),
            "org.freedesktop.NetworkManager.Settings.Connection", "ClearSecrets",
            None, None, Gio.DBusCallFlags.NONE, 10000, None,
        )


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("status", "connect", "disconnect"):
        raise VpnError("Expected status, connect, or disconnect")
    action = sys.argv[1]
    if action == "status":
        return status_report()
    if action == "connect":
        request = json.loads(sys.stdin.readline())
        if not isinstance(request, dict):
            raise VpnError("Enter a username and password")
        address = server_url(str(request.get("server", "")))
        username = str(request.get("username", "")).strip()
        password = request.get("password", "")
        if password is None:
            password = ""
        if not isinstance(password, str):
            raise VpnError("Enter a username and password")
        profile_name = str(request.get("profile", "") or "").strip()
        remember = bool(request.get("remember", False))
        share = bool(request.get("share", False))
        typed_password = password != ""
        if profile_name and profile_name not in known_by_name() and profile_name != PROFILE:
            raise VpnError("Unknown VPN profile")
        if not password:
            if not profile_name:
                raise VpnError("Enter a username and password")
            password = lookup_password(profile_name)
        if (not username or not password
                or any(char in value for value in (username, password) for char in "\r\n")):
            raise VpnError("Enter a username and password")
        if profile_name in known_by_name():
            uuid = ensure_named_profile(profile_name, address, username)
        else:
            uuid = ensure_profile(address, username)
        values = authenticate(address, username, password)
        if remember and typed_password:
            store_typed_password(profile_name, password, share)
        activate(uuid, values)
        if profile_name in known_by_name():
            return status_report()
    elif action == "disconnect":
        disconnect_vpn()
        return status_report()
    return state()


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, **main()}))
    except GLib.Error:
        print(json.dumps({"ok": False, "error": "NetworkManager rejected the VPN operation"}))
        sys.exit(1)
    except (VpnError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
