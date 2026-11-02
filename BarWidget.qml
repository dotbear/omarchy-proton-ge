import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons as Commons
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "dotbear.proton-ge"
  ipcTarget: "dotbear.proton-ge"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  property var state: ({phase: "waiting"})
  property int cursor: 0
  property string actionError: ""
  readonly property bool busy: action.running || ["checking", "downloading", "extracting"].indexOf(state.phase) !== -1
  readonly property string statePath: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state") + "/dotbear.proton-ge/state.json"
  readonly property string helper: decodeURIComponent(Qt.resolvedUrl("proton_ge.py").toString().replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string statusText: {
    var phase = String(state.phase || "waiting")
    var known = ["idle", "checking", "current", "available", "deferred", "downloading", "extracting", "updated", "rollback", "error", "waiting"]
    if (known.indexOf(phase) === -1) phase = "error"
    return strings.t("status." + phase).arg(Commons.I18n.formatNumber(Number(state.progress || 0)))
  }

  function run(command) {
    if (action.running) return
    actionError = ""
    action.command = ["python", root.helper, command]
    action.running = true
  }
  function activate() {
    if (cursor === 0 && !busy) run("check")
    if (cursor === 1 && !busy) run("update")
    if (cursor === 2 && !busy && state.previous) run("rollback")
    if (cursor === 3 && !busy) run(state.auto ? "pause" : "resume")
  }

  FileView {
    id: snapshot
    path: root.statePath
    watchChanges: true
    onFileChanged: reload()
    onLoaded: {
      var raw = text()
      if (raw.length > 32768) return
      try { root.state = JSON.parse(raw) } catch (e) { }
    }
  }

  Process {
    id: action
    stderr: StdioCollector {
      onStreamFinished: root.actionError = String(text || "").slice(0, 500)
    }
    onExited: snapshot.reload()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰊗"
    tooltipText: strings.t("tooltip")
    active: root.state.phase === "error" || root.state.phase === "available" || root.state.phase === "deferred"
    onPressed: function(mouseButton) { if (mouseButton === Qt.LeftButton) root.toggle() }
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.opened
    contentWidth: fittedContentWidth(Style.space(390))
    contentHeight: fittedContentHeight(loadedContentHeight)
    focusTarget: loadedContent
    content: Component {
      PanelKeyCatcher {
        implicitHeight: column.implicitHeight
        onCloseRequested: root.close()
        onMoveRequested: function(dx, dy) { root.cursor = (root.cursor + (dy || dx) + 4) % 4 }
        onTabRequested: function(direction) { root.cursor = (root.cursor + direction + 4) % 4 }
        onActivateRequested: root.activate()

        ColumnLayout {
          id: column
          width: parent.width
          spacing: Style.space(12)

          PlainText {
            text: strings.t("title")
            font.pixelSize: Style.font.heading
            font.bold: true
          }
          PlainText { text: strings.t("installed").arg(root.state.installed || strings.t("notInstalled")) }
          PlainText { text: strings.t("latest").arg(root.state.latest || strings.t("unknown")); color: root.dim }
          PlainText { visible: !!root.state.previous; text: strings.t("previous").arg(root.state.previous || ""); color: root.dim }
          PlainText { text: root.statusText; font.bold: true }
          PlainText { visible: !!root.state.error || !!root.actionError; text: root.actionError || String(root.state.error || ""); color: Commons.Color.urgent }

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.space(6)
            Button {
              text: strings.t("check")
              enabled: !root.busy
              hasCursor: root.cursor === 0
              onClicked: root.run("check")
            }
            Button {
              text: strings.t("update")
              enabled: !root.busy && root.state.latest !== root.state.installed
              hasCursor: root.cursor === 1
              onClicked: root.run("update")
            }
          }
          Button {
            text: strings.t("rollback")
            visible: !!root.state.previous
            enabled: !root.busy
            hasCursor: root.cursor === 2
            onClicked: root.run("rollback")
          }
          Button {
            text: strings.t(root.state.auto ? "autoOn" : "autoOff")
            enabled: !root.busy
            hasCursor: root.cursor === 3
            onClicked: root.run(root.state.auto ? "pause" : "resume")
          }
          PlainText { text: strings.t("network"); color: root.dim; font.pixelSize: Style.font.caption }
          PlainText {
            visible: root.state.selections_pending === true || root.state.selections_ready === true
            text: strings.t(root.state.selections_pending ? "steamPending" : "steamReady")
            color: root.dim
            font.pixelSize: Style.font.caption
          }
          PlainText { visible: root.state.restart_required === true; text: strings.t("restart"); font.pixelSize: Style.font.caption }
        }
      }
    }
  }

  component PlainText: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    Layout.fillWidth: true
    wrapMode: Text.Wrap
  }
}
