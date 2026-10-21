import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "Theme.js" as Theme

Rectangle {
    id: workspace
    objectName: "mailSearchWorkspace"
    color: Theme.surface
    signal backRequested()
    property bool showFullAnswer: false
    property bool showAllResults: false
    property bool showSources: false
    property bool showPossible: false
    property bool showHistory: false
    property int selectedTurn: -1
    property var sessionViews: ({})
    property string displayedViewKey: ""
    property string displayedRevision: ""
    property int sourceDocumentRevision: 0
    Connections {
        target: App
        function onDocumentReady(messageId) {
            if (messageId === workspace.sourceMessage.id) workspace.sourceDocumentRevision += 1
        }
    }
    readonly property var messages: App.chatMessages
    readonly property var answer: currentAnswer()
    readonly property string currentQuestion: questionForAnswer()
    readonly property var citations: answer.citations || []
    readonly property var fileCards: answer.file_cards || (answer.attachments || []).concat(answer.related_attachments || [])
    readonly property var confirmedFiles: fileCards.filter(function(card) { return !card.possible_match })
    readonly property var possibleFiles: fileCards.filter(function(card) { return !!card.possible_match })
    readonly property var confirmedResults: resultRows()
    readonly property bool longAnswer: (answer.text || "").trim().split(/\s+/).length > 120
    readonly property var source: App.sourcePreview
    readonly property bool sourceOpen: !!source.source_id
    readonly property var sourceMessage: (source.messages || [])[0] || ({})
    readonly property var previousTurns: turnsBeforeLatest()
    readonly property var localPreview: App.searchPreview || ({})
    readonly property var pendingMatches: (localPreview.file_cards || []).map(function(card) { return {kind: "file", value: card} }).concat((localPreview.citations || []).map(function(citation) { return {kind: "email", value: citation} })).slice(0, 3)

    function currentAnswer() {
        if (selectedTurn >= 0 && selectedTurn < messages.length) return messages[selectedTurn]
        return messages.length && messages[messages.length - 1].role === "assistant" ? messages[messages.length - 1] : ({})
    }
    function questionForAnswer() {
        for (let i = selectedTurn >= 0 ? selectedTurn - 1 : messages.length - 1; i >= 0; --i)
            if (messages[i].role === "user") return messages[i].text || ""
        return ""
    }
    function turnsBeforeLatest() {
        let result = [], title = ""
        for (let i = 0; i < messages.length; ++i) {
            if (messages[i].role === "user") title = messages[i].text || ""
            if (messages[i].role === "assistant" && i < messages.length - 1) result.push({index: i, title: title || "Earlier answer"})
        }
        return result
    }
    function resultRows() {
        let rows = confirmedFiles.map(function(card) { return {kind: "file", value: card} }), seen = {}
        for (let i = 0; i < citations.length; ++i) {
            const citation = citations[i], key = citation.message_id || citation.app_link || citation.source_id
            // Citation IDs identify chunks, while file IDs identify originals.
            // Keep attachment evidence in Sources rather than duplicating each
            // file as a misleading email row when those IDs differ.
            const represented = (citation.source_type === "attachment" && confirmedFiles.length > 0)
                || confirmedFiles.some(function(card) { return card.id === citation.source_id || (!!card.message_id && card.message_id === citation.message_id) })
            if (!represented && !seen[key]) { rows.push({kind: "email", value: citation}); seen[key] = true }
        }
        return rows
    }
    function answerPreview() {
        const value = answer.text || ""
        if (showFullAnswer || !longAnswer) return value
        // Keep paragraph/list whitespace and complete cited sentences when
        // possible. The original answer remains unchanged behind Show more.
        const words = /\S+/g
        let match, count = 0, cutoff = value.length
        while ((match = words.exec(value)) !== null) {
            if (count++ === 120) { cutoff = match.index; break }
        }
        let preview = value.slice(0, cutoff).trim()
        const endings = /[.!?。！？](?:["'”’])?(?:\s*(?:\[(?:(?:SOURCE|ATTACHMENT):\s*)?[0-9a-fA-F-]{36}\]|cite[^]+))*\s+/g
        let complete = 0
        while ((match = endings.exec(preview + " ")) !== null) {
            const end = Math.min(preview.length, match.index + match[0].length)
            if (preview.slice(0, end).trim().split(/\s+/).length >= 60) complete = end
        }
        if (complete) return preview.slice(0, complete).trim()
        // Never expose a partial citation token if a spaced marker crossed
        // the cutoff. answerTextHtml supplies validated fallback references.
        return preview.replace(/\[(?:(?:SOURCE|ATTACHMENT):)?[^\]]*$/, "")
            .replace(/cite[^]*$/, "").trim() + "…"
    }
    function resetAnswer() {
        showFullAnswer = false; showAllResults = false; showSources = false; showPossible = false; showHistory = false; selectedTurn = -1
        if (resultScroll.contentItem) resultScroll.contentItem.contentY = 0
    }
    function currentViewKey() { return JSON.stringify([App.selectedAccount, App.selectedSearchId]) }
    function answerRevision() {
        const latest = messages.length ? messages[messages.length - 1] : ({})
        return JSON.stringify([messages.length, latest.role, latest.text, latest.citations,
            latest.file_cards || latest.attachments, latest.related_attachments,
            latest.related_citations, latest.insufficient_evidence])
    }
    function saveSessionView() {
        if (displayedViewKey) sessionViews[displayedViewKey] = {
            question: followUp.text, scroll: resultScroll.contentItem.contentY, showSources: showSources,
            showPossible: showPossible, showFullAnswer: showFullAnswer, showAllResults: showAllResults,
            showHistory: showHistory, selectedTurn: selectedTurn
        }
    }
    function restoreSessionView(key) {
        resetAnswer()
        const saved = sessionViews[key]
        followUp.text = saved ? saved.question : ""
        if (saved) {
            showSources = saved.showSources; showPossible = saved.showPossible; showFullAnswer = saved.showFullAnswer
            showAllResults = saved.showAllResults; showHistory = saved.showHistory; selectedTurn = saved.selectedTurn
            Qt.callLater(function() {
                if (workspace.displayedViewKey === key) resultScroll.contentItem.contentY = saved.scroll
            })
        }
    }
    function syncPresentation() {
        const key = currentViewKey(), revision = answerRevision()
        if (displayedViewKey !== key) {
            // A controller notification arrives after its account/session has
            // changed, so save using the identity of the view still on screen.
            saveSessionView()
            displayedViewKey = key
            displayedRevision = revision
            restoreSessionView(key)
        } else if (displayedRevision !== revision) {
            displayedRevision = revision
            resetAnswer()
        }
    }
    function selectSession(id) { saveSessionView(); App.selectSearch(id) }
    function ask(value) { saveSessionView(); App.startSearch(value); followUp.text = ""; followUp.forceActiveFocus() }
    function submitFollowUp() {
        const value = followUp.text.trim()
        if (!value || App.aiBusy) return
        followUp.text = ""
        if (messages.length) App.askFollowUp(value); else App.startSearch(value)
    }
    function openSource(link) {
        if (link && link.startsWith("emailreader://source/")) App.openSearchSource(link)
        else if (link) App.openLink(link)
    }
    function exportFile(card) { exportDialog.attachmentId = card.id; exportDialog.selectedFile = App.defaultExportUrl(card.filename); exportDialog.open() }
    onVisibleChanged: { App.setAssistantVisible(visible); if (visible) followUp.forceActiveFocus(); else saveSessionView() }
    Shortcut {
        sequence: "Escape"; enabled: workspace.visible && !exportDialog.visible && !historyMenu.visible
        onActivated: { if (workspace.sourceOpen) App.closeSearchSource(); else workspace.backRequested() }
    }
    Component.onCompleted: syncPresentation()
    Connections { target: App; function onChatChanged() { workspace.syncPresentation() } }

    ColumnLayout {
        anchors.fill: parent; anchors.margins: 20; spacing: 14
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            AppButton { objectName: "searchBack"; iconName: "back"; text: "Inbox"; flat: true; compact: true; Accessible.name: "Return to Inbox"; onClicked: workspace.backRequested() }
            Label { text: "Search"; color: Theme.text; font.pixelSize: 20; font.weight: Font.DemiBold; Layout.fillWidth: true }
            AppButton {
                objectName: "searchHistory"; text: "History"; iconName: "history"; flat: true; compact: true
                enabled: App.searchSessions.length > 0; onClicked: historyMenu.popup()
                Menu {
                    id: historyMenu; width: 300
                    background: Rectangle { color: Theme.surface; border.color: Theme.controlBorder; radius: 6 }
                    Instantiator {
                        model: App.searchSessions
                        delegate: MenuItem {
                            id: historyEntry
                            required property var modelData
                            text: modelData.title || "New search"; checkable: true; checked: modelData.id === App.selectedSearchId
                            onTriggered: workspace.selectSession(modelData.id)
                            contentItem: Text { text: historyEntry.text; color: Theme.text; font.pixelSize: 14; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; leftPadding: 8 }
                            background: Rectangle { color: historyEntry.highlighted ? Theme.selection : "transparent"; radius: 6 }
                        }
                        onObjectAdded: function(index, object) { historyMenu.insertItem(index, object) }
                        onObjectRemoved: function(index, object) { historyMenu.removeItem(object) }
                    }
                }
            }
            AppButton { objectName: "newSearch"; text: "New search"; compact: true; onClicked: workspace.ask("") }
        }
        Label { text: App.selectedAccount ? "Searching the selected account" : "Searching all accounts"; color: Theme.secondary; font.pixelSize: 13 }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 20
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 300; spacing: 12
                ScrollView {
                    id: resultScroll; objectName: "searchResultsScroll"
                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ColumnLayout {
                        width: resultScroll.availableWidth; spacing: 12
                        Label { visible: workspace.messages.length === 0; text: "Find something in your email"; color: Theme.text; font.pixelSize: 20; font.weight: Font.DemiBold; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Label { visible: workspace.messages.length === 0; text: "Ask for a detail, a document, or a conversation. For example: “Find my auto insurance documents.”"; color: Theme.secondary; font.pixelSize: 14; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        AppButton { visible: workspace.previousTurns.length > 0; text: workspace.showHistory ? "Hide earlier questions" : "Earlier questions (" + workspace.previousTurns.length + ")"; iconName: workspace.showHistory ? "chevron-down" : "chevron-right"; flat: true; compact: true; onClicked: workspace.showHistory = !workspace.showHistory }
                        Repeater {
                            model: workspace.showHistory ? workspace.previousTurns : []
                            delegate: AppButton {
                                required property var modelData
                                Layout.fillWidth: true; flat: true; compact: true; text: modelData.title
                                onClicked: { workspace.resetAnswer(); workspace.selectedTurn = modelData.index; workspace.showHistory = false }
                            }
                        }
                        AppButton { visible: workspace.selectedTurn >= 0; text: "Back to latest answer"; iconName: "back"; flat: true; compact: true; onClicked: workspace.resetAnswer() }
                        Label { objectName: "searchQuestionTitle"; visible: !!workspace.currentQuestion; text: workspace.currentQuestion; textFormat: Text.PlainText; color: Theme.text; font.pixelSize: 18; font.weight: Font.DemiBold; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Label { objectName: "localPreviewLabel"; visible: !workspace.answer.text && workspace.pendingMatches.length > 0; text: "Local matches — relevance not yet checked"; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Repeater {
                            model: !workspace.answer.text ? workspace.pendingMatches : []
                            delegate: ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true; spacing: 4
                                FileRow { visible: modelData.kind === "file"; Layout.fillWidth: true; card: modelData.value; onSourceRequested: function(link) { workspace.openSource(link) }; onExportRequested: function(value) { workspace.exportFile(value) } }
                                AppButton { visible: modelData.kind === "email"; Layout.fillWidth: true; flat: true; compact: true; text: modelData.value.title || "Possible source email"; onClicked: workspace.openSource(modelData.value.app_link || "emailreader://source/" + modelData.value.source_id) }
                            }
                        }
                        Label { objectName: "indexingPendingWarning"; visible: App.indexingPending > 0 && workspace.messages.length > 0; text: App.indexingPending + " files or messages are still being indexed; results may be incomplete."; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Label { objectName: "chatEvidenceWarning"; visible: !!workspace.answer.insufficient_evidence; text: "Evidence is incomplete or conflicting. Check the linked sources before acting."; color: Theme.danger; font.pixelSize: 14; font.weight: Font.DemiBold; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Label {
                            objectName: "searchAnswer"; visible: !!workspace.answer.text
                            text: App.answerTextHtml(workspace.answerPreview(), workspace.citations)
                            textFormat: Text.RichText; color: Theme.text; font.pixelSize: 16
                            wrapMode: Text.WrapAtWordBoundaryOrAnywhere; Layout.fillWidth: true
                            onLinkActivated: function(link) { workspace.openSource(link) }
                        }
                        AppButton { objectName: "showMoreAnswer"; visible: workspace.longAnswer; text: workspace.showFullAnswer ? "Show less" : "Show more"; flat: true; compact: true; onClicked: workspace.showFullAnswer = !workspace.showFullAnswer }
                        AppButton { objectName: "searchSources"; visible: !!workspace.answer.text; text: "Sources (" + workspace.citations.length + ")"; iconName: workspace.showSources ? "chevron-down" : "chevron-right"; flat: true; compact: true; onClicked: workspace.showSources = !workspace.showSources }
                        Repeater {
                            model: workspace.showSources ? workspace.citations : []
                            delegate: ColumnLayout {
                                required property var modelData
                                required property int index
                                Layout.fillWidth: true; spacing: 4
                                AppButton { Layout.fillWidth: true; flat: true; compact: true; text: "[" + (index + 1) + "] " + (modelData.title || "Source email") + (modelData.locator ? " · " + modelData.locator : ""); onClicked: workspace.openSource(modelData.app_link || "emailreader://source/" + modelData.source_id) }
                                Label { text: [modelData.sender, modelData.account_email, modelData.sent_at ? Qt.formatDateTime(new Date(modelData.sent_at), "MMM d, yyyy") : ""].filter(Boolean).join(" · "); textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
                                AppButton { visible: !!modelData.provider_link; text: "Open in mail app"; flat: true; compact: true; iconName: "external"; onClicked: App.openLink(modelData.provider_link) }
                            }
                        }
                        Repeater {
                            model: workspace.showAllResults ? workspace.confirmedResults : workspace.confirmedResults.slice(0, 3)
                            delegate: ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true; spacing: 0
                                FileRow { visible: modelData.kind === "file"; Layout.fillWidth: true; card: modelData.value; onSourceRequested: function(link) { workspace.openSource(link) }; onExportRequested: function(value) { workspace.exportFile(value) } }
                                ColumnLayout {
                                    visible: modelData.kind === "email"; Layout.fillWidth: true; spacing: 4
                                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                                    RowLayout {
                                        Layout.fillWidth: true; spacing: 10
                                        AppIcon { name: "mail"; color: Theme.secondary; Layout.alignment: Qt.AlignTop; Layout.topMargin: 12 }
                                        ColumnLayout {
                                            Layout.fillWidth: true; Layout.topMargin: 8; Layout.bottomMargin: 8; spacing: 3
                                            Label { text: modelData.value.title || "Source email"; textFormat: Text.PlainText; color: Theme.text; font.pixelSize: 14; elide: Text.ElideRight; Layout.fillWidth: true }
                                            Label { text: [modelData.value.sender || modelData.value.account_email, modelData.value.sent_at ? Qt.formatDateTime(new Date(modelData.value.sent_at), "MMM d, yyyy") : ""].filter(Boolean).join(" · "); textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
                                        }
                                        AppButton { text: "Open"; compact: true; flat: true; onClicked: workspace.openSource(modelData.value.app_link || "emailreader://source/" + modelData.value.source_id) }
                                    }
                                }
                            }
                        }
                        AppButton { objectName: "moreConfirmedResults"; visible: workspace.confirmedResults.length > 3; text: workspace.showAllResults ? "Show fewer results" : "Show " + (workspace.confirmedResults.length - 3) + " more results"; flat: true; compact: true; onClicked: workspace.showAllResults = !workspace.showAllResults }
                        AppButton { objectName: "morePossibleSources"; visible: workspace.possibleFiles.length + (workspace.answer.related_citations || []).length > 0; text: workspace.showPossible ? "Hide possible matches" : "Possible matches (" + (workspace.possibleFiles.length + (workspace.answer.related_citations || []).length) + ")"; iconName: workspace.showPossible ? "chevron-down" : "chevron-right"; flat: true; compact: true; onClicked: workspace.showPossible = !workspace.showPossible }
                        Label { visible: workspace.showPossible; text: "Related results that were not confirmed as evidence for this answer."; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Repeater {
                            model: workspace.showPossible ? workspace.possibleFiles : []
                            delegate: FileRow { required property var modelData; Layout.fillWidth: true; card: modelData; onSourceRequested: function(link) { workspace.openSource(link) }; onExportRequested: function(value) { workspace.exportFile(value) } }
                        }
                        Repeater {
                            model: workspace.showPossible ? (workspace.answer.related_citations || []) : []
                            delegate: ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true; spacing: 4
                                AppButton { Layout.fillWidth: true; flat: true; compact: true; text: modelData.title || "Possible source"; onClicked: workspace.openSource(modelData.app_link) }
                                Label { objectName: "relatedCitationExcerpt"; text: App.assistantExcerptHtml(modelData.excerpt || ""); textFormat: Text.RichText; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true; onLinkActivated: function(link) { workspace.openSource(link) } }
                            }
                        }
                        Label { visible: App.aiBusy && !workspace.answer.text; text: "Finding the relevant messages and files…"; color: Theme.secondary; font.pixelSize: 14; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Item { implicitHeight: 8; Layout.fillWidth: true }
                    }
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    TextField {
                        id: followUp; objectName: "chatQuestion"
                        Layout.fillWidth: true; implicitHeight: 40; font.pixelSize: 14
                        color: Theme.text; placeholderTextColor: Theme.secondary; selectionColor: Theme.selection; selectedTextColor: Theme.text
                        placeholderText: workspace.messages.length ? "Ask a follow-up…" : "Ask your email…"
                        background: Rectangle { color: Theme.surface; border.color: followUp.activeFocus ? Theme.accent : Theme.controlBorder; border.width: followUp.activeFocus ? 2 : 1; radius: 10 }
                        onAccepted: workspace.submitFollowUp()
                    }
                    AppButton { objectName: "searchSubmit"; text: "Ask"; primary: true; compact: true; enabled: followUp.text.trim().length > 0 && !App.aiBusy; onClicked: workspace.submitFollowUp() }
                    AppButton { objectName: "searchCancel"; text: "Cancel"; compact: true; visible: App.aiBusy; onClicked: App.cancelAI() }
                }
            }
            Rectangle {
                objectName: "searchSourcePreview"; visible: workspace.sourceOpen
                Layout.preferredWidth: Math.min(440, Math.max(320, workspace.width * 0.42)); Layout.fillHeight: true
                color: Theme.surface; border.color: Theme.border; radius: 6
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 14; spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "Source"; color: Theme.secondary; font.pixelSize: 13; Layout.fillWidth: true }
                        AppButton { objectName: "closeSearchSource"; iconName: "close"; flat: true; compact: true; Accessible.name: "Close source preview"; ToolTip.visible: hovered; ToolTip.text: "Close source preview"; onClicked: App.closeSearchSource() }
                    }
                    Label { text: workspace.source.subject || "Source email"; textFormat: Text.PlainText; color: Theme.text; font.pixelSize: 18; font.weight: Font.DemiBold; wrapMode: Text.Wrap; Layout.fillWidth: true }
                    Label { text: [workspace.sourceMessage.sender_name || workspace.sourceMessage.sender_email, workspace.sourceMessage.sent_at ? Qt.formatDateTime(new Date(workspace.sourceMessage.sent_at), "MMM d, yyyy") : "", workspace.source.locator].filter(Boolean).join(" · "); textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: 12; wrapMode: Text.Wrap; Layout.fillWidth: true }
                    ScrollView {
                        id: sourceScroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        ColumnLayout {
                            width: sourceScroll.availableWidth; spacing: 10
                            Label { visible: workspace.source.read_state === "loading"; text: "Opening source…"; color: Theme.secondary; Layout.fillWidth: true }
                            Label { visible: workspace.source.read_state === "error"; text: workspace.source.error || "The source could not be opened."; color: Theme.danger; wrapMode: Text.Wrap; Layout.fillWidth: true }
                            Loader {
                                Layout.fillWidth: true; Layout.preferredHeight: item ? Math.min(item.implicitHeight, Math.max(180, sourceScroll.height - 100)) : 0
                                active: workspace.sourceOpen && !!workspace.sourceMessage.id && workspace.source.read_state !== "loading"
                                sourceComponent: MailHtmlView { objectName: "searchSourceBody"; width: sourceScroll.availableWidth; scrollTarget: sourceScroll.contentItem; documentHtml: (workspace.sourceDocumentRevision, App.sourceMessageDocument(workspace.sourceMessage.id, false, false, false)) }
                            }
                            Repeater {
                                model: workspace.sourceMessage.file_attachments || (workspace.sourceMessage.attachments || []).filter(function(card) { return !card.is_inline })
                                delegate: FileRow { required property var modelData; Layout.fillWidth: true; card: modelData; onSourceRequested: function(link) { workspace.openSource(link) }; onExportRequested: function(value) { workspace.exportFile(value) } }
                            }
                        }
                    }
                }
            }
        }
    }
    FileDialog { id: exportDialog; property string attachmentId: ""; title: "Export original attachment"; fileMode: FileDialog.SaveFile; onAccepted: App.exportAttachment(attachmentId, selectedFile.toString()) }
}
