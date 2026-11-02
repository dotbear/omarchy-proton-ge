import QtQuick
import Quickshell.Io

Item {
  property var shell: null
  property var manifest: null

  // One worker for the whole shell, regardless of the number of monitors.
  // tick checks locally once a minute; the helper rate-limits network access
  // to once an hour and retries a deferred installation once games exit.
  Timer {
    interval: 60000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: if (!worker.running) worker.running = true
  }

  Process {
    id: worker
    command: ["python", decodeURIComponent(Qt.resolvedUrl("proton_ge.py").toString().replace(/^file:\/\//, "")), "tick"]
  }
}
