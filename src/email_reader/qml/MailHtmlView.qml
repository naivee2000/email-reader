import QtQuick
import QtWebEngine

Item {
    id: root
    property string documentHtml: ""
    property string hoveredLink: ""
    property real documentHeight: 60
    property int measurementPasses: 0
    property bool alive: true
    property var scrollTarget: null
    implicitHeight: Math.max(60, documentHeight)
    // Long messages scroll in the native WebEngine viewport. Leave part of
    // the conversation pane visible so attachments remain reachable below it.
    height: scrollTarget
        ? Math.min(implicitHeight, Math.max(180, scrollTarget.height - 100))
        : implicitHeight

    function refreshDocument() {
        measurementPasses = 0
        documentHeight = 60
        if (documentHtml)
            web.loadHtml(documentHtml, "about:blank")
    }

    function measureDocument() {
        if (!alive || !visible)
            return
        // Measuring the document/body includes this WebEngineView's own height.
        // Feeding that size back into the view made a short mail grow forever.
        // The content wrapper measures only the email's intrinsic layout.
        web.runJavaScript(
            "(() => { let content = document.getElementById('mail-content');" +
            "return content ? Math.ceil(content.getBoundingClientRect().height) : 0; })()",
            function(result) {
                if (!root.alive)
                    return
                let measured = Number(result)
                if (isFinite(measured) && measured > 0)
                    // Leave room for Chromium's fractional image rounding and
                    // the final line box so the last pixels are not clipped.
                    root.documentHeight = Math.max(60, Math.ceil(measured) + 12)
            }
        )
    }

    // Some complex sender templates decode their embedded image after the
    // first native size notification. Poll briefly, with a hard cap, then
    // stop. Never restart from a size change: that could outlive navigation.
    Timer {
        id: settleTimer
        interval: 100
        repeat: true
        onTriggered: {
            if (!root.alive || !root.visible) {
                stop()
                return
            }
            root.measurementPasses += 1
            root.measureDocument()
            if (root.measurementPasses >= 15)
                stop()
        }
    }

    onDocumentHtmlChanged: refreshDocument()
    onWidthChanged: {
        if (width > 0 && documentHtml)
            settleTimer.restart()
    }
    Component.onCompleted: refreshDocument()
    Component.onDestruction: {
        alive = false
        settleTimer.stop()
    }

    function forwardWheel(event) {
        let step = event.pixelDelta.y !== 0
            ? -event.pixelDelta.y
            : -event.angleDelta.y / 120 * 80
        step = Math.max(-1000, Math.min(1000, Math.round(step)))
        if (!step)
            return
        event.accepted = true
        web.runJavaScript(
            "(() => { const before=window.scrollY; window.scrollBy(0," + step + ");" +
            "return [before,window.scrollY,document.documentElement.scrollHeight,window.innerHeight]; })()",
            function(result) {
                if (!root.alive || !root.scrollTarget || !result || result.length !== 4)
                    return
                // After the email reaches an edge, continue into the rest of
                // the conversation so attachments and older headers stay reachable.
                let used = result[1] - result[0]
                let remainder = step - used
                if (Math.abs(remainder) > 1) {
                    let target = root.scrollTarget
                    let maximum = Math.max(0, target.contentHeight - target.height)
                    target.contentY = Math.max(0, Math.min(maximum, target.contentY + remainder))
                }
            }
        )
    }

    WebEngineView {
        id: web
        anchors.fill: parent
        backgroundColor: "transparent"
        focus: true

        // Sender scripts remain blocked by email_document's CSP and sanitizer.
        // The application alone uses runJavaScript to measure intrinsic height.
        settings.javascriptEnabled: true
        settings.autoLoadImages: true
        settings.localContentCanAccessFileUrls: false
        settings.localContentCanAccessRemoteUrls: false
        settings.pluginsEnabled: false
        settings.linksIncludedInFocusChain: true
        settings.screenCaptureEnabled: false


        onLoadingChanged: function(loadRequest) {
            if (loadRequest.status === WebEngineView.LoadSucceededStatus) {
                root.measurementPasses = 0
                settleTimer.restart()
            }
        }
        onLinkHovered: function(url) {
            root.hoveredLink = url ? url.toString() : ""
        }
        onNavigationRequested: function(request) {
            if (request.navigationType === WebEngineNavigationRequest.LinkClickedNavigation) {
                request.action = WebEngineNavigationRequest.IgnoreRequest
                App.openLink(request.url.toString())
            }
        }
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.NoButton
        onWheel: function(event) { root.forwardWheel(event) }
    }
}
