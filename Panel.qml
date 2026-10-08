import QtQuick
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "jeffe747.cisco-vpn"

  property string server: ""
  property string username: ""
  property string profile: ""
  property string profileLabel: ""
  property bool saved: false
  property bool remember: true
  property bool share: true
  property bool credentialsOpen: false
  property bool serverDirty: false
  property bool usernameDirty: false
  property bool profileDirty: false
  property bool applying: false
  property string errorText: ""
  property bool connected: false
  property bool busy: false
  property bool cancelRequested: false
  property string action: ""
  property string request: ""
  property bool dependenciesKnown: false
  property var missingPackages: []
  property var profiles: []
  readonly property string helper: Qt.resolvedUrl("backend.py").toString().replace(/^file:\/\//, "")
  readonly property string dependencyHelper: Qt.resolvedUrl("dependencies.py").toString().replace(/^file:\/\//, "")
  readonly property bool dependenciesReady: dependenciesKnown && missingPackages.length === 0

  implicitWidth: icon.implicitWidth
  implicitHeight: icon.implicitHeight

  function refresh() {
    if (busy || dependencyProcess.running) return
    if (!dependenciesKnown) {
      dependencyProcess.running = true
      return
    }
    if (!dependenciesReady || statusProcess.running) return
    statusProcess.running = true
  }

  function installDependencies() {
    if (busy || dependenciesReady || !dependenciesKnown) return
    busy = true
    action = "install"
    errorText = ""
    installProcess.running = true
  }

  function formIsClean() {
    return !serverDirty && !usernameDirty && !profileDirty
  }

  function isSaved(item) {
    return !!item && (item.saved === true || item.saved === 1)
  }

  function matchedProfile() {
    var serverText = serverField.text.trim()
    var userText = userField.text.trim()
    if (serverText === "" && userText === "") {
      serverText = server.trim()
      userText = username.trim()
    }
    for (var i = 0; i < profiles.length; i++) {
      var item = profiles[i]
      if ((item.server || "").trim() === serverText && (item.username || "").trim() === userText)
        return item
    }
    return null
  }

  function syncSelection() {
    var match = matchedProfile()
    if (!match) {
      saved = false
      return
    }
    profile = match.name
    profileLabel = match.label || match.name
    saved = isSaved(match)
  }

  function selectProfile(item) {
    applying = true
    profile = item.name
    profileLabel = item.label || item.name
    server = item.server
    username = item.username
    serverField.text = item.server
    userField.text = item.username
    saved = isSaved(item)
    profileDirty = true
    serverDirty = false
    usernameDirty = false
    applying = false
    errorText = ""
  }

  function applyResult(text, exitCode, showError) {
    try {
      var data = JSON.parse(text)
      if (!data.ok || exitCode !== 0) {
        if (showError) errorText = data.error || "VPN operation failed"
        return
      }
      connected = data.connected === true
      if (Array.isArray(data.profiles)) profiles = data.profiles
      if (showError && data.connected === true) {
        serverDirty = false
        usernameDirty = false
        profileDirty = false
        errorText = ""
      }
      if (!opened || formIsClean()) {
        applying = true
        if (!serverDirty) {
          server = data.server || ""
          serverField.text = server
        }
        if (!usernameDirty) {
          username = data.username || ""
          userField.text = username
        }
        if (!profileDirty) {
          profile = data.profile || ""
          profileLabel = data.label || ""
        }
        applying = false
      }
      syncSelection()
    } catch (error) {
      if (showError) errorText = "VPN helper returned an invalid response"
    }
  }

  function connectVpn() {
    if (busy) return
    var match = matchedProfile()
    var typed = passwordField.text
    if (!serverField.text.trim() || !userField.text.trim()) {
      errorText = "Enter a server and username"
      return
    }
    if (!typed && !isSaved(match)) {
      credentialsOpen = true
      errorText = "Enter the VPN password"
      Qt.callLater(function() { passwordField.forceActiveFocus() })
      return
    }
    var profileName = match ? match.name : ""
    request = JSON.stringify({
      server: serverField.text.trim(),
      username: userField.text.trim(),
      password: typed,
      profile: profileName,
      remember: remember,
      share: remember && share && profileName !== ""
    })
    busy = true
    cancelRequested = false
    action = "connect"
    errorText = ""
    actionProcess.command = ["python", helper, "connect"]
    actionProcess.running = true
    passwordField.text = ""
  }

  function disconnectVpn() {
    if (busy) return
    busy = true
    action = "disconnect"
    errorText = ""
    actionProcess.command = ["python", helper, "disconnect"]
    actionProcess.running = true
  }

  function cancelVpn() {
    if (!busy || action !== "connect" || cancelRequested) return
    cancelRequested = true
    request = ""
    if (actionProcess.processId) actionProcess.signal(15)
  }

  onOpenedChanged: if (opened) {
    credentialsOpen = false
    dependenciesKnown = false
    profileDirty = false
    serverDirty = false
    usernameDirty = false
    refresh()
  } else {
    credentialsOpen = false
    passwordField.text = ""
    profileDirty = false
    serverDirty = false
    usernameDirty = false
  }

  Timer {
    interval: 5000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Process {
    id: dependencyProcess
    command: ["python", root.dependencyHelper]
    stdout: StdioCollector {
      id: dependencyOutput
      waitForEnd: true
    }
    onExited: function(code) {
      try {
        var result = JSON.parse(dependencyOutput.text)
        if (code !== 0 || result.ok !== true || !Array.isArray(result.missing))
          throw new Error("Invalid dependency check")
        root.missingPackages = result.missing
        root.dependenciesKnown = true
        if (root.dependenciesReady) {
          root.errorText = ""
          root.refresh()
          if (root.opened && root.credentialsOpen) Qt.callLater(function() { passwordField.forceActiveFocus() })
        }
      } catch (error) {
        root.errorText = "Could not check installed packages; run omarchy pkg add networkmanager-openconnect python-gobject in a terminal"
      }
    }
  }

  Process {
    id: installProcess
    command: ["pkexec", "/usr/share/omarchy/bin/omarchy-pkg-add",
              "networkmanager-openconnect", "python-gobject"]
    onExited: function(code) {
      root.busy = false
      root.dependenciesKnown = false
      if (code !== 0)
        root.errorText = "Installation failed or was cancelled; try again or run omarchy pkg add networkmanager-openconnect python-gobject in a terminal"
      root.refresh()
    }
  }

  Process {
    id: statusProcess
    command: ["python", root.helper, "status"]
    stdout: StdioCollector {
      id: statusOutput
      waitForEnd: true
    }
    onExited: function(code) {
      if (code === 0) root.applyResult(statusOutput.text, code, false)
      else if (root.opened && root.errorText === "") root.errorText = "Could not read VPN status"
    }
  }

  Process {
    id: actionProcess
    stdinEnabled: true
    onStarted: {
      if (root.cancelRequested) {
        signal(15)
      } else {
        if (root.request !== "") write(root.request + "\n")
        root.request = ""
      }
    }
    stdout: StdioCollector {
      id: actionOutput
      waitForEnd: true
    }
    onExited: function(code) {
      root.busy = false
      if (root.cancelRequested) root.errorText = "Connection cancelled"
      else root.applyResult(actionOutput.text, code, true)
      root.cancelRequested = false
      root.refresh()
    }
  }

  BarIconButton {
    id: icon
    anchors.fill: parent
    bar: root.bar
    text: root.connected ? "󰌆" : "󰦝"
    tooltipText: root.connected ? ("Cisco VPN connected" + (root.profileLabel ? " · " + root.profileLabel : "")) : "Cisco VPN disconnected"
    onPressed: root.toggle()
  }

  KeyboardPanel {
    anchorItem: icon
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: root.credentialsOpen ? serverField : null
    contentWidth: fittedContentWidth(Style.space(330))
    contentHeight: fittedContentHeight(fields.implicitHeight)

    Column {
      id: fields
      width: parent.width
      spacing: Style.space(8)

      Text {
        text: root.busy ? (root.action === "connect" ? "Cisco VPN · Connecting…" : (root.action === "install" ? "Cisco VPN · Installing packages…" : "Cisco VPN · Disconnecting…")) : (root.connected ? ("Cisco VPN · Connected" + (root.profileLabel ? " · " + root.profileLabel : "")) : "Cisco VPN · Disconnected")
        color: root.barForeground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.body
      }

      Text {
        visible: root.dependenciesReady
        width: parent.width
        wrapMode: Text.WordWrap
        text: "Connects with the saved password."
        color: root.barForeground
        opacity: 0.65
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Repeater {
        model: root.profiles
        delegate: Item {
          required property var modelData
          visible: root.dependenciesReady
          width: fields.width
          height: visible ? Style.space(34) : 0

          Rectangle {
            anchors.fill: parent
            radius: Style.cornerRadius
            color: root.bar ? root.bar.foreground : Color.foreground
            opacity: root.profile === modelData.name ? 0.16 : 0
          }

          Text {
            anchors.left: parent.left
            anchors.leftMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: modelData.label + (modelData.present === false ? " (missing)" : "")
            color: root.barForeground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.body
          }

          Text {
            anchors.right: parent.right
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: modelData.connected ? "Connected" : (modelData.saved ? "Saved" : "")
            color: root.barForeground
            opacity: 0.65
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          MouseArea {
            anchors.fill: parent
            enabled: !root.busy && !root.connected
            cursorShape: Qt.PointingHandCursor
            onClicked: root.selectProfile(modelData)
          }
        }
      }

      Text {
        visible: !root.dependenciesReady && root.dependenciesKnown
        width: parent.width
        wrapMode: Text.WordWrap
        text: "This widget needs " + root.missingPackages.join(" and ") + ". Install the missing packages? Administrator authentication is required."
        textFormat: Text.PlainText
        color: root.barForeground
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.body
      }

      Item {
        visible: root.dependenciesReady
        width: parent.width
        height: credentialLabel.implicitHeight

        Text {
          id: credentialMark
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          text: root.credentialsOpen ? "▾" : "▸"
          color: root.barForeground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.body
        }

        Text {
          id: credentialLabel
          anchors.left: credentialMark.right
          anchors.leftMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: "Server, username, and password"
          color: root.barForeground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.body
        }

        MouseArea {
          anchors.fill: parent
          enabled: !root.busy
          cursorShape: Qt.PointingHandCursor
          onClicked: {
            root.credentialsOpen = !root.credentialsOpen
            if (root.credentialsOpen) Qt.callLater(function() { serverField.forceActiveFocus() })
          }
        }
      }

      TextField {
        id: serverField
        visible: root.dependenciesReady && root.credentialsOpen
        width: parent.width
        placeholderText: "Server (vpn.example.com)"
        text: root.server
        enabled: !root.busy && !root.connected
        onTextEdited: {
          root.server = text
          root.serverDirty = true
          root.profileDirty = false
          root.syncSelection()
        }
        onAccepted: userField.forceActiveFocus()
      }

      TextField {
        id: userField
        visible: root.dependenciesReady && root.credentialsOpen
        width: parent.width
        placeholderText: "Username"
        text: root.username
        enabled: !root.busy && !root.connected
        onTextEdited: {
          root.username = text
          root.usernameDirty = true
          root.profileDirty = false
          root.syncSelection()
        }
        onAccepted: passwordField.forceActiveFocus()
      }

      TextField {
        id: passwordField
        visible: root.dependenciesReady && root.credentialsOpen
        width: parent.width
        placeholderText: root.saved ? "Saved password (leave blank to use it)" : "Password"
        password: true
        enabled: !root.busy && !root.connected
        onAccepted: root.connectVpn()
      }

      Item {
        visible: root.dependenciesReady && root.credentialsOpen
        width: parent.width
        height: rememberLabel.implicitHeight

        Rectangle {
          id: rememberMark
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(16)
          height: Style.space(16)
          radius: 3
          border.width: 1
          border.color: root.barForeground
          color: root.remember ? (root.bar ? root.bar.foreground : Color.foreground) : "transparent"
        }

        Text {
          id: rememberLabel
          anchors.left: rememberMark.right
          anchors.leftMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: "Remember password in the keyring"
          color: root.barForeground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.body
        }

        MouseArea {
          anchors.fill: parent
          enabled: !root.busy && !root.connected
          cursorShape: Qt.PointingHandCursor
          onClicked: root.remember = !root.remember
        }
      }

      Item {
        visible: root.dependenciesReady && root.credentialsOpen && root.remember
        width: parent.width
        height: shareLabel.implicitHeight

        Rectangle {
          id: shareMark
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(16)
          height: Style.space(16)
          radius: 3
          border.width: 1
          border.color: root.barForeground
          color: root.share ? (root.bar ? root.bar.foreground : Color.foreground) : "transparent"
        }

        Text {
          id: shareLabel
          anchors.left: shareMark.right
          anchors.leftMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          width: parent.width - Style.space(24)
          wrapMode: Text.WordWrap
          text: "Save it for all gateways"
          color: root.barForeground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.body
        }

        MouseArea {
          anchors.fill: parent
          enabled: !root.busy && !root.connected
          cursorShape: Qt.PointingHandCursor
          onClicked: root.share = !root.share
        }
      }

      Text {
        visible: root.errorText !== ""
        width: parent.width
        wrapMode: Text.WordWrap
        textFormat: Text.PlainText
        text: root.errorText
        color: root.bar ? root.bar.urgent : Color.urgent
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Rectangle {
        visible: root.dependenciesReady || root.dependenciesKnown
        width: parent.width
        height: Style.space(36)
        radius: Style.cornerRadius
        color: root.bar ? root.bar.foreground : Color.foreground
        opacity: root.busy ? 0.5 : 1

        Text {
          anchors.centerIn: parent
          text: root.busy ? (root.action === "connect" ? (root.cancelRequested ? "Cancelling…" : "Cancel") : "Working…") : (!root.dependenciesReady ? "Install packages" : (root.connected ? "Disconnect" : "Connect"))
          color: Color.background
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
        }
        MouseArea {
          anchors.fill: parent
          enabled: (!root.busy && root.dependenciesKnown) || (root.action === "connect" && !root.cancelRequested)
          cursorShape: Qt.PointingHandCursor
          onClicked: root.busy ? root.cancelVpn() : (!root.dependenciesReady ? root.installDependencies() : (root.connected ? root.disconnectVpn() : root.connectVpn()))
        }
      }
    }
  }
}
