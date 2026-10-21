import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "Theme.js" as Theme

ApplicationWindow {
    id: root
    objectName: "emailReaderWindow"
    visible: true
    width: 1440; height: 900
    minimumWidth: 880; minimumHeight: 620
    title: "Email Reader"
    color: Theme.background
    font.pixelSize: 14
    palette.window: Theme.background
    palette.windowText: Theme.text
    palette.text: Theme.text
    palette.buttonText: Theme.text
    palette.base: Theme.surface
    palette.button: Theme.surface
    palette.highlight: Theme.accent
    palette.highlightedText: Theme.surface
    palette.placeholderText: Theme.secondary
    palette.disabled.text: Theme.secondary
    palette.disabled.windowText: Theme.secondary
    palette.disabled.buttonText: Theme.secondary
    palette.mid: Theme.controlBorder

    property string viewMode: "mail"
    property string selectedThreadId: ""
    property string renderedThreadId: ""
    property string expandedMessageId: ""
    property bool awaitingMessage: false
    property bool newMessageMode: false
    property bool readingMode: false
    property bool highlightDetails: false
    property bool setupChecked: false
    function checkInitialSetup() {
        if (!setupChecked && App.accountsLoaded) {
            setupChecked = true
            if (App.settings.needsSetup) setupDialog.open()
        }
    }
    readonly property bool compact: width < 1100
    readonly property bool narrow: width < 1100
    readonly property int responsiveTextSize: 14
    readonly property int responsiveTitleSize: 20
    readonly property color primaryTextColor: Theme.text
    readonly property color secondaryTextColor: Theme.secondary
    readonly property color accountBadgeTextColor: Theme.accent
    readonly property color accountBadgeBackgroundColor: Theme.selection
    onViewModeChanged: App.setAssistantVisible(viewMode === "search")

    function displayMessageText(value) {
        return value ? value.replace(/\r\n?/g, "\n").split("\n").map(function(line) {
            return line.replace(/^(?: {4,}|\t+)/, "").replace(/[ \t]+$/, "")
        }).join("\n").trim() : ""
    }
    function smartSearch(value) {
        if (!value.trim().length || App.aiBusy) return
        viewMode = "search"
        mailAssistant.ask(value)
    }
    function showMailbox(id) {
        viewMode = "mail"; newMessageMode = false; selectedThreadId = ""
        App.filterMailbox(id)
    }
    function showAccount(id) {
        newMessageMode = false; selectedThreadId = ""
        App.filterAccount(id)
    }
    function startDraft(mode) { App.beginDraft(mode) }

    Connections {
        target: App
        function onAccountsChanged() { root.checkInitialSetup() }
        function onMailRestartApprovalRequired() { mailRecoveryDialog.open() }
        function onSafeToClose() { root.closeAfterWorkReady = true; busyCloseDialog.close(); root.close() }
        function onSelectedThreadChanged() {
            root.selectedThreadId = App.selectedThread.id || ""
            let messages = App.selectedThread.messages || []
            if (root.renderedThreadId === (App.selectedThread.id || "")) {
                if (root.awaitingMessage && messages.length) {
                    root.expandedMessageId = messages[0].id
                    root.awaitingMessage = false
                }
                return
            }
            root.renderedThreadId = App.selectedThread.id || ""
            messageScroll.contentItem.contentY = 0
            root.expandedMessageId = messages.length ? messages[0].id : ""
            root.awaitingMessage = !messages.length
        }
        function onSourceFocusChanged() {
            if (!App.focusedMessageId) return
            root.expandedMessageId = App.focusedMessageId
            App.prepareMessage(App.focusedMessageId)
            Qt.callLater(function() {
                const repeaters = [newestMessageRepeater, olderMessageRepeater]
                for (let group = 0; group < repeaters.length; group++) {
                    for (let index = 0; index < repeaters[group].count; index++) {
                        let card = repeaters[group].itemAt(index)
                        if (card && card.modelData.id === App.focusedMessageId) {
                            messageScroll.contentItem.contentY = Math.max(0, card.y - 20)
                            return
                        }
                    }
                }
            })
        }
    }
    component FlatButton: AppButton {
        id: control
        property color fill: down || hovered ? Theme.hover : "transparent"
        background: Rectangle {
            radius: 6; color: control.fill
            border.width: control.activeFocus ? 2 : 0
            border.color: Theme.accent
        }
    }
    component FitButton: AppButton { flat: false }
    component WrapLabel: Label {
        color: Theme.text
        wrapMode: Text.WrapAtWordBoundaryOrAnywhere
        textFormat: Text.PlainText
    }
    component SectionLabel: Label {
        color: Theme.secondary
        font.pixelSize: 12; font.weight: Font.DemiBold
        elide: Text.ElideRight
    }
    component MessageCard: Rectangle {
        id: messageCard
        objectName: "messageCard-" + modelData.provider_id
        required property var modelData
        property bool showOriginal: false
        property bool showInlineFiles: false
        property bool readingMode: root.readingMode
        property bool highlightDetails: root.highlightDetails
        property int documentRevision: 0
        property bool expanded: root.expandedMessageId === modelData.id
        Connections {
            target: App
            function onDocumentReady(messageId) {
                if (messageId === messageCard.modelData.id) messageCard.documentRevision += 1
            }
        }
        width: parent.width - 32
        height: messageColumn.height + 24
        radius: 0
        border.color: modelData.id === App.focusedMessageId ? Theme.accent : Theme.border
        border.width: modelData.id === App.focusedMessageId ? 2 : 0
        color: "white"
        Column {
            id: messageColumn
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: 12; spacing: 10
            Row {
                width: parent.width
                Label {
                    text: modelData.sender_name || modelData.sender_email
                    textFormat: Text.PlainText
                    font.weight: Font.DemiBold
                    width: Math.max(80, parent.width - (root.compact ? 142 : 180))
                    elide: Text.ElideRight
                }
                Label {
                    text: Qt.formatDateTime(
                        new Date(modelData.sent_at),
                        root.compact ? "MMM d, h:mm AP" : "MMM d, yyyy h:mm AP"
                    )
                    color: Theme.secondary
                    width: root.compact ? 142 : 180
                    horizontalAlignment: Text.AlignRight
                    font.pixelSize: 11
                    elide: Text.ElideLeft
                }
            }
            FlatButton {
                visible: !messageCard.expanded
                text: "Show message"
                Accessible.name: "Expand message from " + (modelData.sender_name || modelData.sender_email)
                onClicked: {
                    root.expandedMessageId = modelData.id
                    App.prepareMessage(modelData.id)
                }
            }
            Label {
                visible: messageCard.expanded
                text: "to " + ((modelData.recipients && modelData.recipients.to) ? modelData.recipients.to.join(", ") : "")
                textFormat: Text.PlainText
                color: Theme.secondary
                font.pixelSize: 11
                width: parent.width
                elide: Text.ElideMiddle
            }
            WrapLabel {
                visible: messageCard.expanded && (modelData.source_complete === 0 || modelData.source_complete === false)
                text: modelData.body_text ? "Showing the saved copy while Mail finishes downloading the original. Refresh will retry." : "Mail has not finished downloading this original. It is queued for a later refresh."
                width: parent.width; color: Theme.secondary
            }
            Loader {
                id: messageBody
                active: messageCard.expanded && modelData.prepared !== false
                visible: active
                property string hoveredLink: item ? item.hoveredLink : ""
                height: item ? Math.min(item.implicitHeight, Math.max(180, messageScroll.contentItem.height - 100)) : 0
                width: parent.width
                sourceComponent: MailHtmlView {
                    objectName: "messageBody-" + messageCard.modelData.provider_id
                    width: messageBody.width
                    height: messageBody.height
                    scrollTarget: messageScroll.contentItem
                    documentHtml: (messageCard.documentRevision, App.messageDocument(
                        messageCard.modelData.id,
                        messageCard.showOriginal,
                        messageCard.readingMode,
                        messageCard.highlightDetails
                    ))
                }
            }
            Label {
                visible: messageCard.expanded && modelData.prepared === false
                text: "Opening message…"
                color: root.secondaryTextColor
            }
            Label {
                visible: messageCard.expanded && messageBody.hoveredLink.length > 0
                width: parent.width
                text: "Link: " + messageBody.hoveredLink
                color: Theme.secondary
                font.pixelSize: Math.max(11, root.responsiveTextSize - 2)
                elide: Text.ElideMiddle
                Accessible.name: text
            }
            FlatButton {
                objectName: "quoteToggle-" + modelData.provider_id
                visible: messageCard.expanded && !!modelData.has_quoted_history
                text: messageCard.showOriginal ? "Hide quoted history" : "Show full message including quoted history"
                onClicked: messageCard.showOriginal = !messageCard.showOriginal
            }
            Row {
                visible: messageCard.expanded && (modelData.remote_image_count > 0 || modelData.remote_image_state === "loading")
                spacing: 6
                FlatButton {
                    visible: modelData.remote_image_state !== "loading"
                    text: (modelData.remote_image_state === "failed" || modelData.remote_image_state === "cancelled" ? "Retry remote images (" : "Remote images blocked — Load (") + modelData.remote_image_count + ")…"
                    Accessible.name: text
                    onClicked: { remoteImageDialog.messageId = modelData.id; remoteImageDialog.open() }
                }
                Label {
                    visible: modelData.remote_image_state === "loading"
                    text: "Loading images " + (modelData.remote_image_completed || 0) + " of " + (modelData.remote_image_total || 0)
                    color: root.secondaryTextColor
                    verticalAlignment: Text.AlignVCenter
                }
                FlatButton {
                    visible: modelData.remote_image_state === "loading"
                    text: "Cancel"
                    onClicked: App.cancelRemoteImages(modelData.id)
                }
            }
            Flow {
                visible: messageCard.expanded
                width: parent.width; spacing: 8
                Repeater {
                    model: messageCard.modelData.file_attachments || []
                    delegate: Row {
                        required property var modelData
                        spacing: 4
                        FitButton {
                            id: attachmentButton
                            iconName: "attachment"
                            text: parent.modelData.filename
                            width: Math.min(implicitWidth, Math.max(120, messageColumn.width - exportButton.implicitWidth - parent.spacing))
                            background: Rectangle { radius: 6; color: attachmentButton.hovered ? Theme.hover : Theme.background }
                            onClicked: App.openAttachment(parent.modelData.id)
                        }
                        FlatButton {
                            id: exportButton
                            objectName: "messageExport-" + parent.modelData.id
                            text: "Export…"
                            enabled: !!parent.modelData.content_digest
                            onClicked: {
                                messageExportDialog.attachmentId = parent.modelData.id
                                messageExportDialog.selectedFile = App.defaultExportUrl(parent.modelData.filename)
                                messageExportDialog.open()
                            }
                        }
                    }
                }
            }
            FlatButton {
                visible: messageCard.expanded && (modelData.inline_attachments || []).length > 0
                text: (messageCard.showInlineFiles ? "Hide" : "Show") + " embedded image originals (" + (modelData.inline_attachments || []).length + ")"
                onClicked: messageCard.showInlineFiles = !messageCard.showInlineFiles
            }
            Flow {
                visible: messageCard.expanded && messageCard.showInlineFiles
                width: parent.width; spacing: 8
                Repeater {
                    model: messageCard.showInlineFiles ? messageCard.modelData.inline_attachments : []
                    delegate: FitButton {
                        required property var modelData
                        text: "Image: " + modelData.filename
                        width: Math.min(implicitWidth, messageColumn.width)
                        onClicked: App.openAttachment(modelData.id)
                    }
                }
            }
        }
    }
    Shortcut { sequences: [StandardKey.Find]; onActivated: searchField.forceActiveFocus() }
    Shortcut { sequences: [StandardKey.New]; onActivated: root.startDraft("new") }

    header: Rectangle {
        height: 56; color: Theme.background
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 12
            spacing: 10
            AppIcon { name: "mail"; color: Theme.accent; size: 22 }
            Label {
                visible: !root.compact
                text: "Email Reader"; font.pixelSize: 18; font.weight: Font.Medium
                color: Theme.text; Layout.preferredWidth: 144
            }
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 38
                radius: 10; color: Theme.surface
                border.color: searchField.activeFocus ? Theme.accent : Theme.controlBorder
                border.width: searchField.activeFocus ? 2 : 1
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 4
                    spacing: 4
                    AppIcon { name: "search" }
                    TextField {
                        id: searchField
                        objectName: "globalSearch"
                        Layout.fillWidth: true
                        placeholderText: "Search your mail or ask a question"
                        color: Theme.text; placeholderTextColor: Theme.secondary
                        background: Item {}
                        selectByMouse: true
                        onAccepted: root.smartSearch(text)
                    }
                    AppButton {
                        objectName: "smartSearchAction"
                        text: "Search"; compact: true
                        enabled: !App.aiBusy && searchField.text.trim().length > 0
                        onClicked: root.smartSearch(searchField.text)
                    }
                    AppButton {
                        iconName: "chevron"; compact: true
                        Accessible.name: "Search options"
                        onClicked: searchOptions.popup()
                        Menu {
                            id: searchOptions
                            MenuItem {
                                text: "Search offline by keyword"
                                objectName: "offlineKeywordAction"
                                enabled: searchField.text.trim().length > 0
                                onTriggered: { root.viewMode = "mail"; root.newMessageMode = false; App.search(searchField.text) }
                            }
                            MenuItem { text: "Search history"; onTriggered: root.viewMode = "search" }
                        }
                    }
                }
            }
            AppButton {
                objectName: "refreshAction"; iconName: "refresh"
                enabled: !App.syncBusy
                Accessible.name: "Refresh mail"
                ToolTip.visible: hovered; ToolTip.text: "Refresh mail"
                onClicked: App.syncAll()
            }
            AppButton {
                iconName: "settings"; Accessible.name: "Settings"
                ToolTip.visible: hovered; ToolTip.text: "Settings"
                onClicked: setupDialog.open()
            }
        }
    }
    footer: Rectangle {
        height: 30; color: Theme.background
        Rectangle { width: parent.width; height: 1; color: Theme.border }
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 8; spacing: 8
            BusyIndicator { visible: App.busy; running: visible; implicitWidth: 16; implicitHeight: 16 }
            Label {
                objectName: "quietStatus"
                text: App.activity.phase === "error" ? "Needs attention — " + App.status : (App.busy ? (App.activity.stage || App.status) : App.status)
                color: App.activity.phase === "error" ? Theme.danger : Theme.secondary
                font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true
            }
            AppButton {
                objectName: "pauseResumeSync"
                visible: App.syncBusy || App.syncPaused
                text: App.syncPaused ? "Resume sync" : "Pause sync"
                compact: true; implicitHeight: 26
                onClicked: App.syncPaused ? App.resumeSync() : App.pauseSync()
            }
            AppButton {
                text: "Details"; compact: true
                implicitHeight: 26
                Accessible.name: "Activity details"
                onClicked: activityDialog.open()
            }
        }
    }

    RowLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            objectName: "navigationSidebar"
            Layout.preferredWidth: root.compact ? 56 : 192
            Layout.fillHeight: true
            color: Theme.background
            ColumnLayout {
                anchors.fill: parent; anchors.margins: root.compact ? 8 : 12
                spacing: 8
                AppButton {
                    objectName: "composeAction"
                    text: root.compact ? "" : "Compose"
                    iconName: "compose"; primary: true
                    Layout.fillWidth: true
                    implicitHeight: 38
                    Accessible.name: "Compose a new message"
                    ToolTip.visible: hovered && root.compact; ToolTip.text: "Compose"
                    onClicked: root.startDraft("new")
                }
                ComboBox {
                    id: accountSelector
                    objectName: "accountSwitcher"
                    visible: !root.compact
                    Layout.fillWidth: true
                    model: [{id: "", email: "All accounts"}].concat(App.accounts)
                    textRole: "email"; valueRole: "id"
                    currentIndex: model.findIndex(function(item) { return item.id === App.selectedAccount })
                    implicitHeight: 34
                    Accessible.name: "Choose email account"
                    background: Rectangle { radius: 6; color: Theme.surface; border.color: Theme.controlBorder }
                    contentItem: Text {
                        text: accountSelector.displayText
                        color: Theme.text; font.pixelSize: 12; elide: Text.ElideMiddle
                        leftPadding: 8; rightPadding: 26; verticalAlignment: Text.AlignVCenter
                    }
                    indicator: AppIcon { name: "chevron"; x: parent.width - width - 6; y: (parent.height-height)/2; size: 16 }
                    onActivated: root.showAccount(currentValue)
                }
                AppButton {
                    visible: root.compact; iconName: "account"; Layout.fillWidth: true
                    Accessible.name: "Choose email account"
                    ToolTip.visible: hovered; ToolTip.text: App.selectedAccount ? "Change account" : "All accounts"
                    onClicked: accountMenu.popup()
                    Menu {
                        id: accountMenu
                        MenuItem { text: "All accounts"; checkable: true; checked: App.selectedAccount === ""; onTriggered: root.showAccount("") }
                        Repeater {
                            model: App.accounts
                            delegate: MenuItem {
                                required property var modelData
                                text: modelData.email; checkable: true; checked: App.selectedAccount === modelData.id
                                onTriggered: root.showAccount(modelData.id)
                            }
                        }
                    }
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                Repeater {
                    model: App.mailboxes
                    delegate: Button {
                        id: nav
                        required property var modelData
                        objectName: "folder-" + modelData.id
                        Layout.fillWidth: true; implicitHeight: 36
                        padding: 8
                        Accessible.name: modelData.label
                        background: Rectangle {
                            radius: 6
                            color: root.viewMode === "mail" && App.selectedMailbox === nav.modelData.id ? Theme.selection : (nav.hovered ? Theme.hover : "transparent")
                            border.width: nav.activeFocus ? 2 : 0
                            border.color: Theme.accent
                        }
                        contentItem: RowLayout {
                            spacing: 10
                            AppIcon { name: nav.modelData.id; color: Theme.text }
                            Label { visible: !root.compact; text: nav.modelData.label; color: Theme.text; Layout.fillWidth: true }
                            Label {
                                visible: !root.compact
                                text: nav.modelData.unread !== undefined ? (nav.modelData.unread || "") : (nav.modelData.count || "")
                                color: Theme.secondary; font.pixelSize: 12
                            }
                        }
                        ToolTip.visible: hovered && root.compact; ToolTip.text: modelData.label
                        onClicked: root.showMailbox(modelData.id)
                    }
                }
                AppButton {
                    Layout.fillWidth: true
                    iconName: "search"; text: root.compact ? "" : "Search history"
                    selected: root.viewMode === "search"
                    Accessible.name: "Search history"
                    ToolTip.visible: hovered && root.compact; ToolTip.text: "Search history"
                    onClicked: root.viewMode = "search"
                }
                Item { Layout.fillHeight: true }
            }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            SplitView {
                id: mailWorkspace
                objectName: "mailWorkspace"
                anchors.fill: parent
                visible: root.viewMode === "mail"
                orientation: Qt.Horizontal
                handle: Rectangle {
                    implicitWidth: 1; color: Theme.border
                }
                Rectangle {
                    SplitView.preferredWidth: 352
                    SplitView.minimumWidth: root.compact ? 280 : 304
                    SplitView.maximumWidth: 480
                    color: Theme.surface
                    ColumnLayout {
                        anchors.fill: parent; spacing: 0
                        RowLayout {
                            Layout.fillWidth: true; Layout.minimumHeight: 44; Layout.maximumHeight: 44
                            Layout.leftMargin: 14; Layout.rightMargin: 14
                            Label { text: App.viewTitle; color: Theme.text; font.weight: Font.DemiBold; Layout.fillWidth: true }
                            Label { text: App.viewCount; color: Theme.secondary; font.pixelSize: 12 }
                        }
                        RowLayout {
                            visible: App.selectedMailbox === "inbox"
                            Layout.fillWidth: true; Layout.minimumHeight: visible ? 36 : 0; Layout.maximumHeight: visible ? 36 : 0
                            spacing: 0
                            Repeater {
                                model: App.categories
                                delegate: Button {
                                    id: category
                                    required property var modelData
                                    Layout.fillWidth: true; implicitHeight: 36
                                    text: modelData.label
                                    background: Rectangle {
                                        color: category.hovered ? Theme.hover : Theme.surface
                                        border.width: category.activeFocus ? 2 : 0
                                        border.color: Theme.accent
                                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 2; color: App.selectedCategory === category.modelData.id ? Theme.accent : Theme.border }
                                    }
                                    contentItem: Text {
                                        text: category.text; color: App.selectedCategory === category.modelData.id ? Theme.accent : Theme.secondary
                                        font.pixelSize: 14; font.weight: App.selectedCategory === category.modelData.id ? Font.DemiBold : Font.Normal
                                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                    }
                                    onClicked: { root.newMessageMode = false; root.selectedThreadId = ""; App.filterCategory(modelData.id) }
                                }
                            }
                        }
                        ListView {
                            id: threadList
                            objectName: "threadList"
                            Layout.fillWidth: true; Layout.fillHeight: true
                            clip: true
                            model: App.threads
                            ScrollBar.vertical: ScrollBar {}
                            delegate: Rectangle {
                                id: threadRow
                                required property var modelData
                                objectName: "threadRow-" + modelData.id
                                width: threadList.width; height: 72
                                activeFocusOnTab: true
                                Accessible.role: Accessible.ListItem
                                Accessible.name: (modelData.latest_sender || modelData.account_email) + ": " + modelData.subject
                                Accessible.description: (modelData.is_unread ? "Unread. " : "Read. ") + modelData.account_email
                                Accessible.selected: root.selectedThreadId === modelData.id
                                Accessible.onPressAction: activate()
                                function activate() {
                                    root.newMessageMode = false
                                    root.selectedThreadId = modelData.id
                                    App.selectThreadAsync(modelData.id)
                                }
                                Keys.onReturnPressed: activate()
                                Keys.onSpacePressed: activate()
                                color: root.selectedThreadId === modelData.id ? Theme.selection : (mouse.containsMouse ? Theme.hover : Theme.surface)
                                Rectangle { width: 2; height: parent.height; color: Theme.accent; visible: root.selectedThreadId === modelData.id || parent.activeFocus }
                                Column {
                                    anchors.fill: parent; anchors.leftMargin: 14; anchors.rightMargin: 12; anchors.topMargin: 7
                                    spacing: 2
                                    RowLayout {
                                        width: parent.width; height: 18; spacing: 6
                                        Label {
                                            text: modelData.latest_sender || modelData.account_email; textFormat: Text.PlainText
                                            color: Theme.text; font.pixelSize: 14
                                            font.weight: modelData.is_unread ? Font.DemiBold : Font.Normal
                                            Layout.fillWidth: true; elide: Text.ElideRight
                                        }
                                        Label {
                                            visible: !App.selectedAccount
                                            text: (modelData.account_email || "").split("@")[0]
                                            Layout.maximumWidth: 60; elide: Text.ElideMiddle
                                            font.pixelSize: 10; color: Theme.secondary
                                            ToolTip.visible: accountHover.containsMouse
                                            ToolTip.text: modelData.account_email
                                            MouseArea { id: accountHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                        }
                                        AppIcon { visible: !!modelData.has_attachments; name: "attachment"; size: 13 }
                                        Label { text: modelData.latest_at ? Qt.formatDateTime(new Date(modelData.latest_at), "MMM d") : ""; color: Theme.secondary; font.pixelSize: 12 }
                                    }
                                    Label { text: modelData.subject; textFormat: Text.PlainText; color: Theme.text; width: parent.width; elide: Text.ElideRight; font.weight: modelData.is_unread ? Font.DemiBold : Font.Normal }
                                    Label { text: root.displayMessageText(modelData.snippet); textFormat: Text.PlainText; width: parent.width; elide: Text.ElideRight; color: Theme.secondary; font.pixelSize: 12 }
                                }
                                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.border }
                                MouseArea { id: mouse; anchors.fill: parent; hoverEnabled: true; onClicked: threadRow.activate() }
                            }
                        }
                    }
                }
                Rectangle {
                    id: readingPane
                    objectName: "readingPane"
                    SplitView.fillWidth: true
                    SplitView.minimumWidth: 400
                    color: Theme.surface
                    ColumnLayout {
                        anchors.fill: parent; spacing: 0
                        ColumnLayout {
                            Layout.fillWidth: true; Layout.margins: 12; spacing: 4
                            Label {
                                text: root.newMessageMode ? "New message" : (App.selectedThread.subject || "Select a conversation")
                                textFormat: Text.PlainText
                                color: App.selectedThread.id || root.newMessageMode ? Theme.text : Theme.secondary
                                font.pixelSize: 20; font.weight: Font.Medium
                                Layout.fillWidth: true; elide: Text.ElideRight
                            }
                            RowLayout {
                                objectName: "readingToolbar"
                                visible: !root.newMessageMode
                                Layout.fillWidth: true; spacing: 2
                                AppButton { objectName: "messageReplyAction"; text: "Reply"; iconName: "reply"; enabled: !!App.selectedThread.id; onClicked: root.startDraft("reply") }
                                AppButton { text: "Reply All"; iconName: readingPane.width >= 520 ? "replyAll" : ""; enabled: !!App.selectedThread.id; onClicked: root.startDraft("reply_all") }
                                AppButton { text: "Forward"; iconName: readingPane.width >= 520 ? "forward" : ""; enabled: !!App.selectedThread.id; onClicked: root.startDraft("forward") }
                                AppButton {
                                    objectName: "readingMore"; text: "More"; iconName: "more"
                                    enabled: !!App.selectedThread.id
                                    onClicked: mailMenu.popup()
                                    Menu {
                                        id: mailMenu
                                        MenuItem { text: "Summary"; enabled: !App.aiBusy; onTriggered: App.summarize() }
                                        MenuItem { text: "Mark unread"; onTriggered: App.markUnread() }
                                        MenuItem { text: App.selectedThread.important_override === 1 ? "Remove Important" : "Mark Important"; onTriggered: App.setImportant(App.selectedThread.id, App.selectedThread.important_override !== 1) }
                                        MenuSeparator {}
                                        MenuItem { text: "Original layout"; checkable: true; checked: !root.readingMode; onTriggered: root.readingMode = false }
                                        MenuItem { text: "Reading layout"; checkable: true; checked: root.readingMode; onTriggered: root.readingMode = true }
                                        MenuItem { text: "Highlight key details"; enabled: root.readingMode; checkable: true; checked: root.highlightDetails; onTriggered: root.highlightDetails = !root.highlightDetails }
                                        MenuItem { text: "Collapse message"; enabled: root.expandedMessageId.length > 0; onTriggered: root.expandedMessageId = "" }
                                    }
                                }
                                Item { Layout.fillWidth: true }
                            }
                            AppButton {
                                visible: manualEditor.active && !manualEditor.visible
                                text: "Resume unsaved draft"; iconName: "drafts"
                                onClicked: manualEditor.open()
                            }
                        }
                        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                    ScrollView {
                        id: messageScroll
                        objectName: "messageScroll"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        contentWidth: availableWidth
                        Column {
                            width: messageScroll.availableWidth
                            height: childrenRect.height + 32
                            padding: 16
                            spacing: 12
                            Label {
                                visible: App.selectedThread.read_state === "loading"
                                text: "Opening conversation…"
                                color: root.secondaryTextColor
                            }
                            Label {
                                visible: App.selectedThread.read_state === "error"
                                text: "This conversation could not be opened. Select it again to retry."
                                color: root.secondaryTextColor
                            }
                            Repeater {
                                id: newestMessageRepeater
                                model: root.newMessageMode ? [] : (App.selectedThread.messages || []).slice(0, 1)
                                delegate: MessageCard {}
                            }

                            DraftEditor {
                                id: manualEditor
                                width: parent.width - 32
                                height: implicitHeight
                                visible: active && (App.editorDraft.mode === "new" ? root.newMessageMode : App.editorDraft.thread_id === App.selectedThread.id)
                                onApplicationCloseApproved: { root.allowApplicationClose = true; root.close() }
                                onActiveChanged: if (!active) root.newMessageMode = false
                                onRevealRequested: {
                                    root.viewMode = "mail"
                                    root.newMessageMode = App.editorDraft.mode === "new"
                                    if (!root.newMessageMode) root.selectedThreadId = App.editorDraft.thread_id
                                    if (!root.newMessageMode && App.editorDraft.thread_id !== App.selectedThread.id)
                                        App.selectThreadAsync(App.editorDraft.thread_id)
                                    Qt.callLater(function() {
                                        messageScroll.contentItem.contentY = Math.max(0, manualEditor.y - 12)
                                    })
                                }
                            }
                            Repeater {
                                id: olderMessageRepeater
                                model: root.newMessageMode ? [] : (App.selectedThread.messages || []).slice(1)
                                delegate: MessageCard {}
                            }
                        }
                    }

                    }
                }
            }
            MailAssistant {
                id: mailAssistant
                anchors.fill: parent
                visible: root.viewMode === "search"
                onBackRequested: root.viewMode = "mail"
            }
        }
    }

    Dialog {
        id: mailRecoveryDialog
        objectName: "mailRecoveryDialog"
        anchors.centerIn: parent
        modal: true; width: Math.min(480, root.width - 40)
        closePolicy: Popup.CloseOnEscape
        onClosed: App.cancelMailRestart()
        background: Rectangle { color: Theme.surface; radius: 10; border.color: Theme.border }
        header: RowLayout {
            Label { text: "Restart unresponsive Apple Mail?"; color: Theme.text; padding: 16; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            AppButton { iconName: "close"; Accessible.name: "Cancel Mail restart"; onClicked: mailRecoveryDialog.close() }
        }
        contentItem: ColumnLayout {
            spacing: 16
            WrapLabel { Layout.fillWidth: true; text: "Apple Mail is not responding, so open drafts cannot be checked. Force quitting may lose unsaved text in Mail. Email Reader will reopen Mail and resume importing. It will not send any email." }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                AppButton { text: "Cancel"; onClicked: mailRecoveryDialog.close() }
                AppButton { text: "Force quit and reopen"; enabled: !App.providerBusy; onClicked: { App.confirmMailRestart(); mailRecoveryDialog.close() } }
            }
        }
    }
    Dialog {
        id: activityDialog
        anchors.centerIn: parent
        modal: true; width: Math.min(480, root.width - 40)
        closePolicy: Popup.CloseOnEscape
        background: Rectangle { color: Theme.surface; radius: 10; border.color: Theme.border }
        header: RowLayout {
            Label { text: "Activity"; color: Theme.text; font.pixelSize: 18; padding: 16; Layout.fillWidth: true }
            AppButton { iconName: "close"; Accessible.name: "Close activity details"; onClicked: activityDialog.close() }
        }
        contentItem: ColumnLayout {
            spacing: 12
            WrapLabel { text: App.status; Layout.fillWidth: true }
            WrapLabel { visible: App.busy || App.activity.phase === "error"; text: App.activity.detail || App.activity.stage || ""; Layout.fillWidth: true }
            ProgressBar { visible: App.busy; Layout.fillWidth: true; indeterminate: !(App.activity.total > 0); from: 0; to: Math.max(1, App.activity.total || 0); value: App.activity.completed || 0 }
            AppButton { visible: App.aiBusy; text: "Cancel AI operation"; onClicked: App.cancelAI() }
            AppButton { visible: App.syncBusy || App.syncPaused; text: App.syncPaused ? "Resume sync" : "Pause sync"; onClicked: App.syncPaused ? App.resumeSync() : App.pauseSync() }
        }
    }
    Dialog {
        id: summaryDialog
        objectName: "summaryDialog"
        anchors.centerIn: parent
        width: Math.min(600, root.width - 40)
        height: Math.min(650, root.height - 60)
        modal: true
        visible: App.assistantResult.kind === "summary"
        closePolicy: Popup.CloseOnEscape
        onClosed: App.closeAssistant()
        background: Rectangle { color: Theme.surface; radius: 10; border.color: Theme.border }
        header: RowLayout {
            Label { text: "Summary"; color: Theme.text; font.pixelSize: 20; padding: 16; Layout.fillWidth: true }
            AppButton { iconName: "close"; Accessible.name: "Close summary"; onClicked: App.closeAssistant() }
        }
        contentItem: ScrollView {
            id: summaryScroll
            contentWidth: availableWidth
            clip: true
            Column {
                width: summaryScroll.availableWidth; spacing: 14
                WrapLabel {
                    visible: (App.assistantResult.omitted_count || 0) > 0
                    text: "Some points were omitted because their source could not be verified."
                    width: parent.width; color: Theme.secondary
                }
                Repeater {
                    model: [
                        {title: "Key points", values: App.assistantResult.key_points || []},
                        {title: "Decisions", values: App.assistantResult.decisions || []},
                        {title: "Dates and deadlines", values: App.assistantResult.dates_and_deadlines || []},
                        {title: "Actions — verify current status", values: App.assistantResult.action_items || []},
                        {title: "Attachments", values: App.assistantResult.important_attachments || []}
                    ]
                    delegate: Column {
                        required property var modelData
                        visible: modelData.values.length > 0
                        width: parent.width; spacing: 6
                        Label { text: modelData.title; color: Theme.text; font.weight: Font.DemiBold }
                        Repeater {
                            model: modelData.values
                            delegate: WrapLabel {
                                required property string modelData
                                width: parent.width
                                textFormat: Text.RichText
                                text: App.assistantTextHtml("• " + modelData, App.assistantResult.citations || [])
                                onLinkActivated: function(link) { App.openLink(link) }
                            }
                        }
                    }
                }
            }
        }
    }
    Dialog {
        id: imagePreviewDialog
        objectName: "imagePreviewDialog"
        anchors.centerIn: parent
        modal: true
        width: Math.min(root.width - 40, 1100)
        height: Math.min(root.height - 40, 820)
        closePolicy: Popup.CloseOnEscape
        property bool fitImage: true
        visible: !!App.previewAttachment.id
        onClosed: App.closeAttachmentPreview()
        header: RowLayout {
            Label {
                text: App.previewAttachment.filename || "Image preview"
                color: root.primaryTextColor
                font.weight: Font.DemiBold
                padding: 14
                elide: Text.ElideMiddle
                Layout.fillWidth: true
            }
            FlatButton {
                objectName: "imagePreviewZoom"
                text: imagePreviewDialog.fitImage ? "100%" : "Fit"
                Accessible.name: imagePreviewDialog.fitImage ? "View image at 100 percent" : "Fit image to window"
                onClicked: imagePreviewDialog.fitImage = !imagePreviewDialog.fitImage
            }
            FlatButton {
                objectName: "imagePreviewOpenOriginal"
                text: "Open original"
                onClicked: App.openAttachment(App.previewAttachment.id)
            }
            FlatButton {
                objectName: "imagePreviewClose"
                iconName: "close"
                Accessible.name: "Close image preview"
                onClicked: imagePreviewDialog.close()
            }
        }
        contentItem: ScrollView {
            id: previewScroll
            clip: true
            contentWidth: imagePreviewDialog.fitImage ? availableWidth : previewImage.implicitWidth
            contentHeight: imagePreviewDialog.fitImage ? availableHeight : previewImage.implicitHeight
            Image {
                id: previewImage
                objectName: "imagePreviewImage"
                source: App.previewAttachment.source || ""
                asynchronous: true
                cache: true
                fillMode: Image.PreserveAspectFit
                width: imagePreviewDialog.fitImage ? previewScroll.availableWidth : implicitWidth
                height: imagePreviewDialog.fitImage ? previewScroll.availableHeight : implicitHeight
                Accessible.name: App.previewAttachment.filename || "Image preview"
            }
        }
    }
    property bool allowApplicationClose: false
    property bool closeAfterWorkReady: false
    Shortcut {
        sequence: "Escape"
        enabled: App.assistantResult.kind === "summary" && !manualEditor.active && !setupDialog.visible && !remoteImageDialog.visible
        onActivated: App.closeAssistant()
    }
    onClosing: function(close) {
        if (App.draftSaving) {
            close.accepted = false
            busyCloseDialog.open()
        } else if (!allowApplicationClose && manualEditor.active && manualEditor.dirty) {
            close.accepted = false
            manualEditor.closeApplication = true
            manualEditor.requestClose()
        } else if (!closeAfterWorkReady && App.busy) {
            close.accepted = false
            busyCloseDialog.open()
        }
    }

    Dialog {
        id: busyCloseDialog
        anchors.centerIn: parent
        modal: true; width: Math.min(450, root.width - 40)
        closePolicy: Popup.CloseOnEscape
        header: RowLayout {
            Label { text: "An operation is still finishing"; color: root.primaryTextColor; padding: 16; Layout.fillWidth: true }
            FlatButton { iconName: "close"; Accessible.name: "Keep app open"; onClicked: busyCloseDialog.close() }
        }
        contentItem: ColumnLayout {
            WrapLabel { Layout.fillWidth: true; text: "You can stop background work at its next safe checkpoint and close. Saved mail and import progress will be retained." }
            FlatButton { objectName: "closeAfterCheckpoint"; text: "Stop safely and close"; onClicked: { App.requestCloseWhenSafe(); busyCloseDialog.close() } }
            FlatButton { visible: App.aiBusy; text: "Cancel AI and keep editing"; onClicked: { App.cancelAI(); busyCloseDialog.close() } }
            FlatButton { text: "Keep app open"; onClicked: busyCloseDialog.close() }
        }
    }

    Dialog {
        id: remoteImageDialog
        property string messageId: ""
        anchors.centerIn: parent
        width: Math.min(460, root.width - 48)
        modal: true
        closePolicy: Popup.CloseOnEscape
        standardButtons: Dialog.Ok | Dialog.Cancel
        header: RowLayout {
            Label { text: "Load remote images?"; color: root.primaryTextColor; padding: 16; Layout.fillWidth: true }
            FlatButton { iconName: "close"; Accessible.name: "Close remote image request"; onClicked: remoteImageDialog.reject() }
        }
        contentItem: WrapLabel {
            text: "Loading images contacts the sender’s image servers and may reveal that you opened this email. Images stay in memory for this conversation; no cookies are shared."
        }
        onAccepted: App.loadRemoteImages(messageId)
    }

    Dialog {
        id: setupDialog
        title: "Private setup"
        closePolicy: Popup.CloseOnEscape
        header: RowLayout {
            Label { text: "Private setup"; color: root.primaryTextColor; padding: 16; font.pixelSize: 20; Layout.fillWidth: true }
            FlatButton { iconName: "close"; Accessible.name: "Close setup"; onClicked: setupDialog.close() }
        }
        modal: true
        width: Math.min(720, root.width - 32)
        height: Math.min(700, root.height - 32)
        anchors.centerIn: parent
        standardButtons: Dialog.Close
        ScrollView {
            id: setupScroll
            anchors.fill: parent
            contentWidth: availableWidth
            clip: true
            ColumnLayout {
            width: setupScroll.availableWidth; spacing: 10
            WrapLabel { text: "Secrets are saved in macOS Keychain. Local Codex uses your existing ChatGPT login; relevant excerpts still leave this Mac and remain subject to your ChatGPT workspace data controls."; Layout.fillWidth: true; color: Theme.secondary }
            SectionLabel { text: "AI BACKEND" }
            ComboBox {
                id: aiBackend
                Layout.fillWidth: true
                model: ["Automatic — API key, then local Codex", "Local Codex — ChatGPT sign-in", "OpenAI API key"]
                currentIndex: App.settings.aiBackend === "codex" ? 1 : (App.settings.aiBackend === "api" ? 2 : 0)
            }
            WrapLabel {
                text: App.settings.localCodexAvailable
                      ? "✓ Codex CLI detected. Active backend: " + App.settings.activeAiBackend
                      : "Codex CLI was not detected; install it or use an API key."
                Layout.fillWidth: true
                color: App.settings.localCodexAvailable ? "#137333" : Theme.danger
            }
            TextField { id: openaiKey; Layout.fillWidth: true; placeholderText: App.settings.openaiConfigured ? "API key already configured (optional)" : "OpenAI API key (optional)"; echoMode: TextInput.Password }
            TextField { id: responseModel; Layout.fillWidth: true; text: App.settings.responseModel || "gpt-5.6-terra"; placeholderText: "Response model" }
            RowLayout {
                Label { text: "Monthly hard limit (USD)" }
                SpinBox { id: limitBox; from: 0; to: 10000; value: App.settings.monthlyLimit || 25 }
            }
            FitButton {
                text: "Save AI settings"
                onClicked: App.saveSetup(
                    openaiKey.text,
                    aiBackend.currentIndex === 1 ? "codex" : (aiBackend.currentIndex === 2 ? "api" : "auto"),
                    googleId.text,
                    googleSecret.text,
                    microsoftId.text,
                    microsoftTenant.text,
                    responseModel.text,
                    limitBox.value
                )
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.border }
            SectionLabel { text: "APPLE MAIL — NO DEVELOPER KEYS" }
            WrapLabel { text: "Keep mail searchable across accounts, including Updates, mailing lists, social mail, and forums. Only explicit Promotions, Junk, and Trash are excluded. No provider developer keys are required."; Layout.fillWidth: true; color: Theme.secondary }
            WrapLabel { text: "Inbox and Sent are checked on each refresh. Drafts are rechecked every 30 minutes because Apple Mail cannot reliably report draft edits; use Check Drafts now after editing one."; Layout.fillWidth: true; color: root.secondaryTextColor }
            RowLayout {
                ComboBox { id: appleRangeBox; model: ["1 year", "5 years", "all", "custom"] }
                TextField { id: appleCustomDate; placeholderText: "YYYY-MM-DD"; visible: appleRangeBox.currentText === "custom" }
                FitButton { text: "Import from Apple Mail"; enabled: !App.providerBusy; onClicked: App.importAppleMail(appleRangeBox.currentText, appleCustomDate.text) }
                FitButton { text: "Check Drafts now"; enabled: !App.providerBusy; onClicked: { setupDialog.close(); App.refreshDraftsNow() } }
            }
            WrapLabel {
                Layout.fillWidth: true
                visible: App.pendingOriginalCount > 0
                text: App.pendingOriginalCount + " original files are waiting for Apple Mail. Automatic retries back off when Mail cannot provide them."
                color: root.secondaryTextColor
            }
            FitButton {
                text: "Retry missing originals now"
                visible: App.pendingOriginalCount > 0
                enabled: !App.providerBusy
                onClicked: { setupDialog.close(); App.retryOriginalsNow() }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.border }
            CheckBox { id: advancedOAuth; text: "Show advanced direct-provider OAuth settings" }
            SectionLabel { text: "GOOGLE DESKTOP OAUTH"; visible: advancedOAuth.checked }
            TextField { id: googleId; Layout.fillWidth: true; placeholderText: "Google client ID"; visible: advancedOAuth.checked }
            TextField { id: googleSecret; Layout.fillWidth: true; placeholderText: "Google client secret"; echoMode: TextInput.Password; visible: advancedOAuth.checked }
            SectionLabel { text: "MICROSOFT PUBLIC CLIENT"; visible: advancedOAuth.checked }
            TextField { id: microsoftId; Layout.fillWidth: true; placeholderText: "Microsoft client ID"; visible: advancedOAuth.checked }
            TextField { id: microsoftTenant; Layout.fillWidth: true; text: "common"; placeholderText: "Tenant"; visible: advancedOAuth.checked }
            RowLayout {
                visible: advancedOAuth.checked
                ComboBox { id: providerBox; model: ["gmail", "outlook"] }
                ComboBox { id: rangeBox; model: ["1 year", "5 years", "all", "custom"] }
                TextField { id: customDate; placeholderText: "YYYY-MM-DD"; visible: rangeBox.currentText === "custom" }
                FitButton { text: "Connect and sync"; enabled: !App.providerBusy; onClicked: App.connectAccount(providerBox.currentText, rangeBox.currentText, customDate.text) }
            }
            WrapLabel { text: "Direct OAuth is optional. Apple Mail accounts are processed together in the same Unified Inbox."; visible: advancedOAuth.checked; Layout.fillWidth: true; color: Theme.secondary }
            Item { Layout.preferredHeight: 8 }
            }
        }
        Component.onCompleted: root.checkInitialSetup()
    }

    FileDialog {
        id: messageExportDialog
        property string attachmentId: ""
        title: "Export original attachment"
        fileMode: FileDialog.SaveFile
        onAccepted: App.exportAttachment(attachmentId, selectedFile.toString())
    }
}
