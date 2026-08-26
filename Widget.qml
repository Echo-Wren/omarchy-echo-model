import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// echo.model — Echo usage, DeepSeek balance, and a model switcher: one bar
// icon and one panel. All data comes from collect.py next to this file, which
// relays the Echo usage bridge (192.168.2.41:8643) into a single JSON state
// file this panel watches.
Panel {
  id: root
  moduleName: "echo.model"
  ipcTarget: "echo.model"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color accent: Color.accent
  readonly property color track: Style.selectedFillFor(foreground, accent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateDir: (Quickshell.env("XDG_STATE_HOME") || home + "/.local/state") + "/echo-model"
  readonly property string stateFile: stateDir + "/stats.json"
  readonly property string scriptPath: String(Qt.resolvedUrl("collect.py")).replace(/^file:\/\//, "")
  readonly property url iconSource: Qt.resolvedUrl("assets/hermes-icon.png")

  property var stats: null
  property bool refreshing: false
  property string applyingModel: ""
  property bool cursorActive: false
  property int modelCursor: 0

  readonly property var api: stats && stats.api ? stats.api : null
  readonly property var usage: stats && stats.usage ? stats.usage : null
  readonly property var hermes: stats && stats.hermes ? stats.hermes : null
  readonly property var keyUsage: api && api.keyUsage ? api.keyUsage : null
  readonly property var lastSessions: usage && Array.isArray(usage.recentSessions) ? usage.recentSessions : []
  readonly property var models: stats && Array.isArray(stats.models) ? stats.models : []
  readonly property int profileCount: hermes ? Math.max(1, Number(hermes.profileCount || 1)) : 1
  readonly property string profileScope: profileCount === 1 ? "1 Hermes profile" : profileCount + " Hermes profiles"

  readonly property string currentModel: hermes ? String(hermes.model || "") : ""
  readonly property string updatedAt: stats ? String(stats.updated || "") : ""
  readonly property real remaining: api && api.ok && isFinite(api.remaining) ? api.remaining : -1
  readonly property real funded: api && api.ok && isFinite(api.total) ? api.total : 0
  readonly property real spent: api && api.ok && isFinite(api.used) ? api.used : 0
  // The meter shows the USED fraction of the topped-up balance (grows as
  // credits are consumed), while the alarm fires on the remaining fraction.
  readonly property real ratio: funded > 0 ? clamp(spent / funded, 0, 1) : -1
  readonly property bool alarming: remaining >= 0 && funded > 0 && (remaining / funded) <= 0.1

  // The bar sizes the slot from the widget root's implicit size. Match the
  // native icon slot (27px) so the button's fixedWidth/iconCanvas center
  // properly. The button must have visual content (text or iconComponent) —
  // BarIconButton's hasVisualContent gates whether the bar renders it at all.
  implicitWidth: Style.bar.iconSlot
  implicitHeight: Style.bar.iconSlot

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  function val(o, k, fallback) {
    return o && o[k] !== undefined && o[k] !== null ? o[k] : fallback
  }

  function fmtTokens(n) {
    var v = Number(n || 0)
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B"
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M"
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "k"
    return String(Math.round(v))
  }

  function fmtMoney(f) {
    var v = Number(f || 0)
    return "$" + v.toFixed(2)
  }

  function fmtCtx(n) {
    var v = Number(n || 0)
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B"
    if (v >= 1e6) return (v / 1e6).toFixed(0) + "M"
    if (v >= 1e3) return (v / 1e3).toFixed(0) + "k"
    return v > 0 ? String(v) : ""
  }

  // Preferred cost for a summary card: the bridge's spent figure for
  // this key when available, else the local Hermes estimate.
  // kind: keyUsage field name ("daily"|"weekly"|"total"|"monthly").
  function cardCost(kind, estimate) {
    var k = root.keyUsage
    if (k && isFinite(Number(k[kind]))) return Number(k[kind])
    return Number(estimate || 0)
  }

  // "deepseek/deepseek-v4-flash-0731" -> "deepseek-v4-flash-0731"
  function shortModel(id) {
    var s = String(id || "")
    var slash = s.lastIndexOf("/")
    return slash >= 0 ? s.slice(slash + 1) : s
  }

  function p2(n) {
    n = String(n)
    return n.length < 2 ? "0" + n : n
  }

  function isoDate(d) {
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate())
  }

  function todayDate() {
    return isoDate(new Date())
  }

  function heroTitle() {
    if (currentModel !== "") return shortModel(currentModel)
    return "Hermes"
  }

  function providerLabel() {
    var p = hermes && hermes.provider ? String(hermes.provider) : "deepseek"
    if (p === "openrouter") return "OpenRouter"
    if (p === "deepseek") return "DeepSeek"
    return p.charAt(0).toUpperCase() + p.slice(1)
  }

  function heroMeta() {
    var label = root.providerLabel()
    if (!api || !api.ok) return label + " · " + (api && api.configured ? "bridge unreachable" : "no data")
    return label + " · " + fmtMoney(remaining) + " remaining"
  }

  function statusText() {
    if (api && api.configured && !api.ok)
      return "Echo usage bridge unreachable — balance unavailable. Usage and the model list still work."
    return ""
  }

  function pricingText(m) {
    if (!m) return ""
    var p = String(val(m, "prompt", ""))
    var c = String(val(m, "completion", ""))
    if (p === "" && c === "") return "free"
    var parts = []
    if (p !== "") parts.push("in " + p)
    if (c !== "") parts.push("out " + c)
    return parts.join(" · ")
  }

  function weekPeak() {
    var peak = 0
    if (usage && Array.isArray(usage.byDay))
      for (var i = 0; i < usage.byDay.length; i++)
        peak = Math.max(peak, Number(usage.byDay[i].tokens || 0))
    return Math.max(1, peak)
  }

  function weekday(date) {
    var parsed = new Date(String(date || "") + "T00:00:00")
    if (isNaN(parsed.getTime())) return ""
    return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][parsed.getDay()]
  }

  function dayLabel(date) {
    var s = String(date || "")
    if (s === root.todayDate()) return "Today"
    var wd = root.weekday(s)
    return wd === "" ? s : wd
  }

  function shortTime(iso) {
    var m = String(iso || "").match(/T(\d\d):(\d\d)/)
    return m ? m[1] + ":" + m[2] : ""
  }

  function refreshNow() {
    if (refreshing) return
    refreshing = true
    collectProcess.command = ["python3", root.scriptPath]
    collectProcess.running = true
  }

  function applyStats(c) {
    var parsed = null
    try { parsed = JSON.parse(String(c || "")) } catch (e) { }
    if (parsed && typeof parsed === "object") root.stats = parsed
  }

  function applyModel(id) {
    if (id === "" || id === root.applyingModel) return
    // The switch is POSTed to the Echo usage bridge /model endpoint by the
    // local `echo-model` script (which carries the switch token). Only accept
    // well-formed model ids — this rejects newlines and anything outside a
    // safe charset that a compromised model listing could inject.
    id = String(id)
    if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$/.test(id)) return
    root.applyingModel = id
    applyProcess.command = ["bash", "-c", "exec ~/.local/bin/echo-model " + id]
    applyProcess.running = true
  }

  function selectCursor(index) {
    if (root.models.length === 0) return
    root.modelCursor = ((index % root.models.length) + root.models.length) % root.models.length
  }

  Process {
    id: collectProcess
    running: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.refreshing = false
    }
  }

  Process {
    id: applyProcess
    running: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.applyingModel = ""
        root.refreshNow()
      }
    }
  }

  FileView {
    id: statsFile
    path: root.stateFile
    watchChanges: true
    printErrors: false
    onLoaded: root.applyStats(text())
    onFileChanged: reload()
  }

  Timer {
    id: refreshTimer
    interval: Math.max(30, Number(root.setting("refreshIntervalSec", 300))) * 1000
    running: true
    repeat: true
    onTriggered: root.refreshNow()
  }

  onStatsChanged: {
    if (root.modelCursor >= root.models.length)
      root.modelCursor = Math.max(0, root.models.length - 1)
  }

  Component.onCompleted: root.refreshNow()
  onOpenedChanged: if (root.opened) root.refreshNow()

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refreshNow(); return "ok" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""  // glyph hidden; the logo renders via iconComponent
    iconComponent: Component {
      Image {
        anchors.fill: parent
        anchors.margins: 1.5  // 13px in the 16px canvas — matches glyph size
        source: Qt.resolvedUrl("assets/hermes-icon.png")
        sourceSize: Qt.size(128, 128)
        fillMode: Image.PreserveAspectFit
        smooth: true
      }
    }
    active: root.alarming
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) { if (root.bar) root.bar.run("xdg-open https://platform.deepseek.com/usage") }
      else if (buttonCode === Qt.MiddleButton) root.refreshNow()
      else root.toggle()
    }
  }

  // PopupCard (xdg-popup, anchored to the bar surface) instead of KeyboardPanel
  // (layer-shell PanelWindow): KeyboardPanel reparents its window across monitor
  // bars when toggled on multi-monitor setups, tripping Qt's "Cannot use same
  // item on different windows" and segfaulting Quickshell on click. PopupCard is
  // what every stock Omarchy bar widget uses. Tradeoff: no keyboard focus prime,
  // so the keyCatcher is inert until the popup itself is clicked.
  PopupCard {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: panel.fittedContentWidth(Style.space(392))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(660))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      visible: true

      onMoveRequested: function(dx, dy) {
        if (dx !== 0) { root.cursorActive = true; root.selectCursor(root.modelCursor + dx) }
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(88), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onActivateRequested: {
        var entry = root.models[root.modelCursor]
        if (entry) root.applyModel(String(entry.id))
      }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refreshNow()
        else if (t === "q" || t === "Q") root.close()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: contentColumn
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: root.heroTitle()
            meta: root.heroMeta()
            foreground: root.foreground
            fontFamily: root.fontFamily

            iconComponent: Component {
              Item {
                width: Style.font.display
                height: Style.font.display
                Image {
                  anchors.centerIn: parent
                  width: Style.font.display
                  height: Style.font.display
                  source: root.iconSource
                  sourceSize: Qt.size(128, 128)
                  fillMode: Image.PreserveAspectFit
                  smooth: true
                }
              }
            }
          }

          // ---------- Status / auth help ----------
          Rectangle {
            visible: root.statusText() !== ""
            width: parent.width
            implicitHeight: statusText.implicitHeight + Style.space(24)
            radius: Style.cornerRadius
            color: root.alpha(urgent, 0.10)
            border.width: 1
            border.color: root.alpha(urgent, 0.35)

            Text {
              id: statusText
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              text: root.statusText()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          // ---------- Balance ----------
          PanelSeparator { visible: root.api !== null && root.api.ok; foreground: root.foreground }

          Column {
            id: balanceColumn
            visible: root.api !== null && root.api.ok
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              width: parent.width
              text: "BALANCE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Item {
              width: parent.width
              implicitHeight: Math.max(balanceLabel.implicitHeight, balanceValue.implicitHeight)

              Text {
                id: balanceLabel
                text: "Credits remaining"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
              }

              Text {
                id: balanceValue
                text: root.fmtMoney(root.remaining)
                color: root.alarming ? urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
              }
            }

            Meter {
              width: parent.width
              value: root.ratio
              alarming: root.alarming
            }

            Text {
              width: parent.width
              text: root.api && root.api.ok
                ? root.fmtMoney(root.api.used) + " spent of " + root.fmtMoney(root.funded) + " funded"
                : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              width: parent.width
              text: "Whole " + root.providerLabel() + " account — includes all API keys"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              opacity: 0.8
            }
          }

          // ---------- Quick stats ----------
          PanelSeparator { visible: root.usage !== null; foreground: root.foreground }

          Row {
            id: statRow
            visible: root.usage !== null
            width: parent.width
            spacing: Style.space(8)

            Repeater {
              model: root.usage ? [
                { title: "TODAY", tokens: root.usage.today.tokens, cost: root.cardCost("daily", root.usage.today.cost) },
                { title: "7 DAYS", tokens: root.usage.week.tokens, cost: root.cardCost("weekly", root.usage.week.cost) },
                { title: "30 DAYS", tokens: root.usage.month30.tokens, cost: root.cardCost("monthly", root.usage.month30.cost) }
              ] : []

              StatCard {
                required property var modelData
                width: (statRow.width - statRow.spacing * 2) / 3
                label: modelData.title
                tokens: modelData.tokens
                cost: modelData.cost
              }
            }
          }

          Text {
            width: parent.width
            visible: root.keyUsage !== null
            text: root.providerLabel() + " balance · usage from the Echo bridge"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            width: parent.width
            visible: root.keyUsage === null
            text: "Consolidated local estimates · tokens from " + root.profileScope
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // ---------- Tokens by day ----------
          PanelSeparator { foreground: root.foreground }
          PanelSectionHeader {
            width: parent.width
            text: "TOKENS BY DAY · ESTIMATED"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.usage && root.usage.byDay ? root.usage.byDay : []
            DayRow {
              required property var modelData
              width: contentColumn.width
              day: modelData
              ratio: Number(modelData.tokens || 0) / root.weekPeak()
            }
          }

          // ---------- Tokens by model (30d) ----------
          PanelSeparator { foreground: root.foreground }
          PanelSectionHeader {
            width: parent.width
            text: "MODELS · 30 DAYS · ESTIMATED"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.usage && root.usage.byModel ? root.usage.byModel : []
            ModelUsageRow {
              required property var modelData
              width: contentColumn.width
              row: modelData
            }
          }

          // ---------- Model switcher ----------
          PanelSeparator { foreground: root.foreground }
          PanelSectionHeader {
            width: parent.width
            text: "SWITCH MODEL"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            id: modelList
            model: root.models

            ModelOption {
              required property var modelData
              required property int index

              width: contentColumn.width
              rowIndex: index
              modelId: String(modelData.id || "")
              sub: root.pricingText(modelData)
              ctx: root.fmtCtx(root.val(modelData, "context", 0))
              selected: String(modelId) === root.currentModel
              optionCursor: root.cursorActive && index === root.modelCursor
              applying: String(modelId) === root.applyingModel
            }
          }

          Text {
            width: parent.width
            visible: root.applyingModel !== ""
            text: "Switching to " + root.shortModel(root.applyingModel) + "…"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }

          Text {
            width: parent.width
            text: "Switches Echo's model through the usage bridge.\nmodel.default — new sessions use it; open sessions keep theirs."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
          }

          // ---------- Recent sessions ----------
          PanelSeparator {
            visible: root.lastSessions.length > 0
            foreground: root.foreground
          }
          PanelSectionHeader {
            visible: root.lastSessions.length > 0
            width: parent.width
            text: "RECENT SESSIONS · ALL PROFILES"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.lastSessions
            SessionRow {
              required property var modelData
              width: contentColumn.width
              row: modelData
            }
          }

          // ---------- Footer ----------
          Text {
            width: parent.width
            topPadding: Style.space(4)
            text: "r refresh · Enter apply · ←/→ switch · Esc close"
              + (root.updatedAt !== "" ? "   ·   " + root.shortTime(root.updatedAt) : "")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }
        }
      }
    }
  }

  // ------------------------------------------------------------ components

  // Small stat card: label over tokens + cost.
  component StatCard: Item {
    id: card
    property string label: ""
    property real tokens: 0
    property real cost: 0

    implicitHeight: cardTitle.implicitHeight + cardBody.implicitHeight + Style.space(2)

    Text {
      id: cardTitle
      anchors.left: parent.left
      anchors.top: parent.top
      text: card.label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    Text {
      id: cardBody
      anchors.left: parent.left
      anchors.top: cardTitle.bottom
      anchors.topMargin: Style.space(2)
      text: root.fmtTokens(card.tokens) + "  " + root.fmtMoney(card.cost)
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }
  }

  // Rounded meter showing the used fraction of the topped-up balance.
  component Meter: Item {
    id: meter
    property real value: -1
    property bool alarming: false
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))

    implicitHeight: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meter.value >= 0 ? meterTrack.height : 0
      radius: meterTrack.radius
      width: meterTrack.width * root.clamp(meter.value, 0, 1)
      color: meter.alarming ? root.urgent : root.foreground

      Behavior on width {
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }
    }
  }

  // One row per day: weekday, bar, token count, spend. The bar runs
  // between the weekday label and the token count, and the spend column
  // lines up with the MODELS section's cost column.
  component DayRow: Item {
    id: dayRow
    property var day: null
    property real ratio: 0

    readonly property bool today: String(dayRow.day ? dayRow.day.date : "") === root.todayDate()

    implicitHeight: Math.max(dayLabelText.implicitHeight,
                             Math.max(dayValue.implicitHeight, dayCost.implicitHeight))
                   + Style.space(2)

    Text {
      id: dayLabelText
      text: root.dayLabel(dayRow.day ? dayRow.day.date : "")
      color: dayRow.today ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: dayRow.today
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(52)
    }

    Text {
      id: dayCost
      text: root.fmtMoney(dayRow.day ? dayRow.day.cost : 0)
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      horizontalAlignment: Text.AlignRight
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(64)
    }

    Text {
      id: dayValue
      text: root.fmtTokens(dayRow.day ? dayRow.day.tokens : 0)
      color: dayRow.today ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: dayRow.today
      horizontalAlignment: Text.AlignRight
      anchors.right: dayCost.left
      anchors.rightMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
    }

    Rectangle {
      id: dayTrack
      anchors.left: dayLabelText.right
      anchors.leftMargin: Style.space(8)
      anchors.right: dayValue.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      height: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))
      radius: height / 2
      color: root.track

      Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: parent.height
        radius: parent.radius
        width: parent.width * root.clamp(dayRow.ratio, 0, 1)
        color: root.alpha(root.foreground, 0.6)

        Behavior on width {
          NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
        }
      }
    }
  }

  // One model row: name, tokens, estimated spend.
  component ModelUsageRow: Item {
    id: modelRow
    property var row: null

    implicitHeight: modelName.implicitHeight + Style.space(8)

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: root.alpha(root.foreground, 0.05)
    }

    Text {
      id: modelName
      text: row && row.model ? root.shortModel(String(row.model)) : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
      anchors.left: parent.left
      anchors.leftMargin: Style.space(8)
      anchors.right: modelTokens.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: modelTokens
      text: row ? root.fmtTokens(row.tokens) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignRight
      anchors.right: modelCost.left
      anchors.rightMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: modelCost
      text: row ? root.fmtMoney(row.cost) : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }
  }

  // One model selector row: id, context, per-1M price; click switches.
  component ModelOption: Item {
    id: option
    property int rowIndex: -1
    property string modelId: ""
    property string sub: ""
    property string ctx: ""
    property bool selected: false
    property bool optionCursor: false
    property bool applying: false

    implicitHeight: Style.space(30)

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: option.optionCursor ? root.track : root.alpha(root.foreground, option.selected ? 0.16 : 0.05)

      Behavior on color {
        ColorAnimation { duration: 120 }
      }
    }

    Text {
      id: optionId
      text: option.modelId === "" ? "—" : root.shortModel(option.modelId)
      color: option.applying ? root.dim : root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: option.selected
      elide: Text.ElideRight
      anchors.left: parent.left
      anchors.leftMargin: Style.space(8)
      anchors.right: optionContext.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: optionContext
      text: option.ctx
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.right: optionPrice.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: optionPrice
      text: option.sub
      color: option.selected ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.right: optionCheck.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: optionCheck
      text: option.applying ? "⟳" : (option.selected ? "✓" : " ")
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      onClicked: {
        root.cursorActive = true
        root.modelCursor = option.rowIndex
        root.applyModel(option.modelId)
      }
    }
  }

  // One recent session row: title, model, start time, cost.
  component SessionRow: Item {
    id: sessionRow
    property var row: null

    implicitHeight: sessionTitle.implicitHeight + Style.space(2)

    Text {
      id: sessionTitle
      text: row ? "[" + String(row.profile || "default") + "] " + row.title : ""
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
      anchors.left: parent.left
      anchors.right: sessionTime.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: sessionTime
      text: row ? row.started : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.right: sessionCost.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
    }

    Text {
      id: sessionCost
      text: row ? root.fmtMoney(row.cost) : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
    }
  }
}