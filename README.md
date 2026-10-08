# Omarchy Cisco VPN

A bar widget for connecting to Cisco AnyConnect-compatible VPNs on
[Omarchy](https://omarchy.org/). OpenConnect authenticates to the gateway,
NetworkManager manages the tunnel, and a Quickshell popup shows the status and
connect/disconnect controls.

## Install

Install the widget from this repository:

```sh
omarchy plugin add https://github.com/Jeffe747/omarchy-cisco-vpn.git --enable
```

Click the VPN icon in the right-hand bar section. On first use, if
`networkmanager-openconnect` or `python-gobject` is missing, the widget offers
an **Install packages** button. Click it to authorize the installation through
Omarchy's graphical administrator prompt. The `omarchy plugin add` command
does not install packages itself. If graphical authorization is unavailable,
install the dependencies in a terminal instead:

```sh
omarchy pkg add networkmanager-openconnect python-gobject
```

Then open the widget again. Choose a gateway, or enter an HTTPS VPN server,
username, and password, then choose **Connect**. Leave the password blank to
use the copy in the login keyring. While connecting, use **Cancel** to stop
the attempt. When connected, the button becomes **Disconnect**. The server
can be entered as a hostname or an HTTPS URL.


## Remove

Disconnect in the widget first if the VPN is active, then remove the plugin:

```sh
omarchy plugin remove jeffe747.cisco-vpn
```

Removing the plugin does not disconnect an active VPN, uninstall system
packages, or delete the `Omarchy Cisco VPN` NetworkManager profile. The
profile may also be used by an earlier local version of this widget.

## Credentials and limitations

The server and username are stored in the NetworkManager profile; autoconnect
is disabled. The password is sent over stdin to OpenConnect for one
authentication attempt and is not saved in the profile or passed as a command
argument. If **Remember password in the keyring** is checked, the password is
written to the login keyring only after the gateway accepts it. **Save it for
all gateways** stores that same password for every profile listed in
`~/.config/omarchy/cisco-vpn-profiles.json`. Leave the password blank later
to reuse the saved one. Uncheck **Save it for all gateways** to update only
the selected profile. A short-lived session cookie is passed through
NetworkManager's in-memory profile and cleared after the attempt. The popup
does not print raw OpenConnect errors because those may contain sensitive
details. The helper stops a subprocess if its combined output exceeds 1 MiB.
Never commit your VPN server details, credentials, NetworkManager profile, or
logs to this repository.

A JSON list at `~/.config/omarchy/cisco-vpn-profiles.json` can prefill
gateways. That file stays on the machine and is not part of this repository.
The popup can connect with a saved password, and the server, username, and
password stay behind a disclosure. VPN groups, OTP/MFA prompts, browser SSO,
client certificates, and custom certificate trust are not supported.
Certificate validation is never disabled. A successful login depends on your
gateway's authentication policy.

## Development

Run the backend tests on Arch with `python-gobject` and NetworkManager
installed:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
omarchy plugin validate .
```

The shell loads `Panel.qml` directly from this repository when installed.
Changes to files in an installed plugin trigger an Omarchy shell reload.
For a QML edit that does not take effect after hot-reload, run
`omarchy restart shell`.

Licensed under [MIT](LICENSE).
