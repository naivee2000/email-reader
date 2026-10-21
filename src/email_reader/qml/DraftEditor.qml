import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

Item {
    id: editor
    objectName: "draftEditorDialog"
    implicitWidth: 400
    implicitHeight: editorContent.implicitHeight + 32
    visible: active
    property bool active: false
    property string originalForm: ""
    property string candidateLabel: "Manual"
    property string sendingAccountId: ""
    property string draftThreadId: ""
    property bool showAssistant: false
    property bool showCc: false
    property bool showBcc: false
    property bool dirty: active && originalForm !== JSON.stringify(form())
    property bool closeApplication: false
    signal applicationCloseApproved()
    signal revealRequested()

    function form() {
        return {account_id: sendingAccountId, to: toField.text, cc: ccField.text,
                bcc: bccField.text, subject: subject.text, body: body.text, candidate_label: candidateLabel}
    }
    function open() { active = true; revealRequested() }
    function close() { active = false; showAssistant = false }
    function requestClose() {
        if (App.draftSaving) return
        if (dirty) confirmClose.open()
        else finishClose()
    }
    function finishClose() {
        App.discardEditor()
        editor.close()
        if (closeApplication) applicationCloseApproved()
    }
    function restoreAccount() { account.currentIndex = account.indexOfValue(sendingAccountId) }
    Shortcut {
        sequence: "Escape"; enabled: editor.active && editor.visible && !confirmClose.visible
        onActivated: {
            if (toField.dismissSuggestions() || ccField.dismissSuggestions() || bccField.dismissSuggestions()) return
            if (editor.showAssistant) editor.showAssistant = false
            else editor.requestClose()
        }
    }
    Connections {
        target: App
        ignoreUnknownSignals: true
        function onAccountsChanged() { Qt.callLater(editor.restoreAccount) }
        function onEditorChanged() {
            if (!App.editorDraft.mode) return
            if (editor.active) { editor.revealRequested(); return }
            const draft = App.editorDraft
            editor.sendingAccountId = draft.account_id || ""
            editor.draftThreadId = draft.thread_id || ""
            editor.restoreAccount()
            toField.text = draft.to || ""; ccField.text = draft.cc || ""; bccField.text = draft.bcc || ""
            subject.text = draft.subject || ""; body.text = draft.body || ""
            editor.showCc = !!ccField.text; editor.showBcc = !!bccField.text
            editor.candidateLabel = "Manual"; instruction.text = ""
            editor.showAssistant = false; editor.closeApplication = false
            editor.originalForm = JSON.stringify(editor.form())
            editor.open()
            Qt.callLater(function() { body.forceActiveFocus() })
        }
        function onEditorCandidatesChanged() { candidateTabs.currentIndex = 0 }
        function onEditorSaved() {
            editor.originalForm = JSON.stringify(editor.form())
            confirmClose.close(); editor.close()
            if (editor.closeApplication) editor.applicationCloseApproved()
        }
    }
    Rectangle { anchors.fill: parent; color: Theme.surface }
    Rectangle { anchors.left: parent.left; anchors.right: parent.right; height: 1; color: Theme.border }
    ColumnLayout {
        id: editorContent
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        anchors.margins: 16; spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Label {
                text: App.editorDraft.mode === "forward" ? "Forward" : (App.editorDraft.mode === "new" ? "New message" : (App.editorDraft.mode === "reply_all" ? "Reply all" : "Reply"))
                color: Theme.text; font.pixelSize: 18; font.weight: Font.DemiBold; Layout.fillWidth: true
            }
            AppButton {
                objectName: "closeDraftEditor"; iconName: "close"; compact: true; flat: true
                enabled: !App.draftSaving
                Accessible.name: "Close draft editor"; ToolTip.text: "Close draft editor"; ToolTip.visible: hovered
                onClicked: editor.requestClose()
            }
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            Label { text: "From"; color: Theme.secondary; font.pixelSize: 14; Layout.preferredWidth: 54 }
            ComboBox {
                id: account; objectName: "editorAccount"; Layout.fillWidth: true; implicitHeight: 34
                enabled: !App.draftSaving
                model: App.accounts; textRole: "email"; valueRole: "id"; font.pixelSize: 14
                leftPadding: 10; rightPadding: 30; Accessible.name: "Sending account"
                onActivated: editor.sendingAccountId = currentValue || ""
                contentItem: Text { text: account.displayText; color: Theme.text; font: account.font; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
                background: Rectangle { color: Theme.surface; radius: 6; border.color: account.activeFocus ? Theme.accent : Theme.controlBorder }
                indicator: AppIcon {
                    objectName: "editorAccountIndicator"; name: "chevron"; color: Theme.secondary; size: 18
                    x: account.width - width - 8; y: (account.height - height) / 2
                }
                delegate: ItemDelegate {
                    id: accountChoice
                    required property var modelData
                    required property int index
                    width: account.width; text: modelData.email; font.pixelSize: 14
                    contentItem: Text { text: accountChoice.modelData.email; color: Theme.text; font: accountChoice.font; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { color: accountChoice.highlighted ? Theme.selection : Theme.surface }
                    highlighted: account.highlightedIndex === index
                }
                popup.background: Rectangle { color: Theme.surface; radius: 6; border.color: Theme.controlBorder }
            }
        }
        AddressField { id: toField; fieldName: "editorTo"; label: "To"; accountId: editor.sendingAccountId; enabled: !App.draftSaving; Layout.fillWidth: true }
        RowLayout {
            Layout.fillWidth: true; Layout.leftMargin: 62; spacing: 4
            AppButton { objectName: "toggleEditorCc"; text: "Cc"; compact: true; flat: true; onClicked: editor.showCc = !editor.showCc }
            AppButton { objectName: "toggleEditorBcc"; text: "Bcc"; compact: true; flat: true; onClicked: editor.showBcc = !editor.showBcc }
            Item { Layout.fillWidth: true }
        }
        AddressField { id: ccField; visible: editor.showCc || !!text; fieldName: "editorCc"; label: "Cc"; accountId: editor.sendingAccountId; enabled: !App.draftSaving; Layout.fillWidth: true }
        AddressField { id: bccField; visible: editor.showBcc || !!text; fieldName: "editorBcc"; label: "Bcc"; accountId: editor.sendingAccountId; enabled: !App.draftSaving; Layout.fillWidth: true }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            Label { text: "Subject"; color: Theme.secondary; font.pixelSize: 14; Layout.preferredWidth: 54 }
            TextField {
                id: subject; objectName: "editorSubject"; Layout.fillWidth: true; implicitHeight: 34
                readOnly: App.draftSaving
                placeholderText: "Subject"; font.pixelSize: 14; selectByMouse: true
                color: Theme.text; placeholderTextColor: Theme.secondary; selectionColor: Theme.selection; selectedTextColor: Theme.text
                background: Rectangle { color: Theme.surface; radius: 6; border.color: subject.activeFocus ? Theme.accent : Theme.controlBorder }
            }
        }
        ScrollView {
            Layout.fillWidth: true; Layout.preferredHeight: 220; clip: true
            TextArea {
                id: body; objectName: "manualDraftBody"; wrapMode: TextEdit.Wrap
                readOnly: App.draftSaving
                color: Theme.text; placeholderTextColor: Theme.secondary; selectionColor: Theme.selection; selectedTextColor: Theme.text
                font.pixelSize: 16; placeholderText: "Write your message…"; padding: 12; selectByMouse: true
                background: Rectangle { color: Theme.surface; radius: 6; border.color: body.activeFocus ? Theme.accent : Theme.controlBorder }
            }
        }
        Label {
            visible: (App.editorDraft.attachment_ids || []).length > 0
            text: (App.editorDraft.attachment_ids || []).length + " original files included"
            color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            AppButton { objectName: "reviewedDraftSave"; text: App.draftSaving ? "Saving draft…" : "Save draft"; primary: true; enabled: !!editor.sendingAccountId && !App.draftSaving; onClicked: App.saveEditor(editor.form()) }
            AppButton { objectName: "replyAssistantToggle"; text: "Reply Assistant"; onClicked: editor.showAssistant = !editor.showAssistant }
            Item { Layout.fillWidth: true }
        }
        Label { text: App.draftSaving ? "Queued / saving draft; nothing sent. You can keep browsing mail." : "Save to Drafts, then send later from your mail app."; color: Theme.secondary; font.pixelSize: 12; wrapMode: Text.Wrap; Layout.fillWidth: true }
        ColumnLayout {
            visible: editor.showAssistant; Layout.fillWidth: true; spacing: 10
            Rectangle { color: Theme.border; Layout.fillWidth: true; Layout.preferredHeight: 1; Layout.topMargin: 6 }
            RowLayout {
                Layout.fillWidth: true
                Label { text: "Reply Assistant"; color: Theme.text; font.pixelSize: 16; font.weight: Font.DemiBold; Layout.fillWidth: true }
                AppButton {
                    objectName: "closeReplyAssistant"; iconName: "close"; compact: true; flat: true
                    Accessible.name: "Close reply suggestions"; ToolTip.text: "Close reply suggestions"; ToolTip.visible: hovered
                    onClicked: editor.showAssistant = false
                }
            }
            TextField {
                id: instruction; objectName: "replyInstruction"; Layout.fillWidth: true; implicitHeight: 36; font.pixelSize: 14
                color: Theme.text; placeholderTextColor: Theme.secondary; selectionColor: Theme.selection; selectedTextColor: Theme.text
                placeholderText: "What should the reply say?"; selectByMouse: true
                background: Rectangle { color: Theme.surface; radius: 6; border.color: instruction.activeFocus ? Theme.accent : Theme.controlBorder }
                onAccepted: if (!App.aiBusy && !App.draftSaving) App.assistEditor(editor.form(), instruction.text)
            }
            RowLayout {
                Layout.fillWidth: true
                AppButton { objectName: "createReplyOptions"; text: "Create three options"; enabled: !App.aiBusy && !App.draftSaving; onClicked: App.assistEditor(editor.form(), instruction.text) }
                AppButton { text: "Cancel"; visible: App.aiBusy; onClicked: App.cancelAI() }
                Item { Layout.fillWidth: true }
            }
            Label { text: "Your writing changes only when you use a reply."; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
            TabBar {
                id: candidateTabs; objectName: "replyCandidateTabs"; visible: App.editorCandidates.length > 0; Layout.fillWidth: true
                background: Rectangle { color: Theme.surface }
                Repeater {
                    model: App.editorCandidates.slice(0, 3)
                    TabButton {
                        id: candidateTab
                        required property var modelData
                        text: modelData.label; font.pixelSize: 14; implicitHeight: 38
                        background: Rectangle {
                            color: candidateTab.hovered ? Theme.hover : Theme.surface
                            border.width: candidateTab.activeFocus ? 2 : 0
                            border.color: Theme.accent
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 2; color: candidateTab.checked ? Theme.accent : Theme.border }
                        }
                        contentItem: Text { text: candidateTab.text; font: candidateTab.font; color: candidateTab.checked ? Theme.accent : Theme.secondary; elide: Text.ElideRight; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    }
                }
            }
            StackLayout {
                Layout.fillWidth: true; currentIndex: candidateTabs.currentIndex; visible: App.editorCandidates.length > 0
                Repeater {
                    model: App.editorCandidates.slice(0, 3)
                    ColumnLayout {
                        id: optionContent
                        required property var modelData
                        objectName: "replyCandidate-" + modelData.label; spacing: 10
                        Label {
                            objectName: "replyCandidateStance-" + optionContent.modelData.label
                            text: optionContent.modelData.assumption || optionContent.modelData.stance || ""
                            visible: !!text; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true
                        }
                        ScrollView {
                            Layout.fillWidth: true; Layout.preferredHeight: 170; clip: true
                            TextArea {
                                objectName: "replyCandidateBody-" + optionContent.modelData.label
                                text: optionContent.modelData.body; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true
                                color: Theme.text; selectedTextColor: Theme.text; selectionColor: Theme.selection
                                font.pixelSize: 16; wrapMode: TextEdit.Wrap; padding: 0
                                background: Rectangle { color: Theme.surface }
                            }
                        }
                        AppButton {
                            objectName: "useOption-" + optionContent.modelData.label; text: "Use this reply"; primary: true
                            enabled: !App.draftSaving
                            onClicked: { body.text = optionContent.modelData.body; editor.candidateLabel = optionContent.modelData.label; body.forceActiveFocus() }
                        }
                    }
                }
            }
        }
    }
    Dialog {
        id: confirmClose; objectName: "confirmCloseDraft"; parent: Overlay.overlay
        modal: true; anchors.centerIn: parent; width: Math.min(400, parent ? parent.width - 32 : 400)
        padding: 16; closePolicy: Popup.CloseOnEscape; onRejected: editor.closeApplication = false
        background: Rectangle { color: Theme.surface; radius: 10; border.color: Theme.controlBorder }
        header: RowLayout {
            Label { text: "Keep this draft?"; color: Theme.text; font.pixelSize: 18; padding: 16; Layout.fillWidth: true }
            AppButton { iconName: "close"; compact: true; flat: true; Accessible.name: "Keep editing"; Layout.rightMargin: 10; onClicked: { editor.closeApplication = false; confirmClose.close() } }
        }
        contentItem: ColumnLayout {
            spacing: 16
            Label { text: "You have unsaved changes."; color: Theme.secondary; font.pixelSize: 14 }
            RowLayout {
                AppButton { text: "Save draft"; primary: true; enabled: !App.draftSaving; onClicked: App.saveEditor(editor.form()) }
                AppButton { objectName: "discardDraft"; text: "Discard"; enabled: !App.draftSaving; onClicked: { confirmClose.close(); editor.finishClose() } }
                AppButton { objectName: "cancelCloseDraft"; text: "Cancel"; onClicked: { editor.closeApplication = false; confirmClose.close() } }
            }
        }
    }
}
