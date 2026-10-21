import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

ColumnLayout {
    id: row
    property var card: ({})
    property bool expanded: false
    readonly property bool available: card.available !== undefined ? !!card.available : !!card.content_digest
    readonly property string accountEmail: {
        const account = (App.accounts || []).find(function(item) { return item.id === row.card.account_id })
        return card.account_email || (account ? account.email : "")
    }
    signal sourceRequested(string link)
    signal exportRequested(var card)
    objectName: "attachmentCard-" + (card.id || "")
    spacing: 4
    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.border }
    RowLayout {
        Layout.fillWidth: true; spacing: 8
        AppIcon { name: "file"; color: Theme.secondary; Layout.alignment: Qt.AlignTop; Layout.topMargin: 12 }
        ColumnLayout {
            Layout.fillWidth: true; Layout.topMargin: 8; Layout.bottomMargin: 8; spacing: 3
            Label { text: row.card.filename || "Attachment"; textFormat: Text.PlainText; font.pixelSize: 14; font.weight: Font.Medium; color: Theme.text; elide: Text.ElideRight; Layout.fillWidth: true; ToolTip.visible: fileHover.hovered; ToolTip.text: text; HoverHandler { id: fileHover } }
            Label { text: [row.card.possible_match ? "Possible match" : row.accountEmail || "Attachment", row.card.email_date ? Qt.formatDateTime(new Date(row.card.email_date), "MMM d, yyyy") : ""].filter(Boolean).join(" · "); textFormat: Text.PlainText; color: Theme.secondary; font.pixelSize: 12; elide: Text.ElideRight; Layout.fillWidth: true }
        }
        AppButton { objectName: "openFile-" + (row.card.id || ""); text: "Open"; flat: true; compact: true; enabled: row.available; onClicked: App.openAttachment(row.card.id) }
        AppButton { objectName: "fileDetails-" + (row.card.id || ""); iconName: row.expanded ? "chevron-down" : "chevron-right"; flat: true; compact: true; Accessible.name: "Details for " + (row.card.filename || "attachment"); ToolTip.visible: hovered; ToolTip.text: "Details"; onClicked: row.expanded = !row.expanded }
    }
    ColumnLayout {
        visible: row.expanded; Layout.fillWidth: true; Layout.leftMargin: 26; Layout.bottomMargin: 10; spacing: 6
        Label { visible: !row.available; text: "The original has not been downloaded yet."; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { visible: !!row.card.identical_original_cited; text: "An identical original is cited. This copy has a more descriptive filename."; color: Theme.secondary; font.pixelSize: 13; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Flow {
            Layout.fillWidth: true; spacing: 6
            AppButton { objectName: "fileSource-" + (row.card.id || ""); text: "Source email"; flat: true; compact: true; onClicked: row.sourceRequested("emailreader://source/" + row.card.id) }
            AppButton { text: "Export…"; flat: true; compact: true; enabled: row.available; onClicked: row.exportRequested(row.card) }
            AppButton { text: "Open in mail app"; flat: true; compact: true; visible: !!row.card.provider_link; onClicked: App.openLink(row.card.provider_link) }
        }
        Label { visible: (row.card.document_dates || []).length > 0; text: "Printed document dates · verify the original"; color: Theme.secondary; font.pixelSize: 12; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Repeater {
            model: row.card.document_dates || []
            delegate: AppButton { required property var modelData; objectName: "documentDate-" + modelData.source_id + "-" + modelData.label; Layout.fillWidth: true; flat: true; compact: true; text: modelData.label + ": " + modelData.value + (modelData.locator ? " · " + modelData.locator : ""); onClicked: row.sourceRequested(modelData.app_link) }
        }
        Repeater {
            model: row.card.duplicate_sources || []
            delegate: AppButton { required property var modelData; objectName: "duplicateSource-" + modelData.id; Layout.fillWidth: true; flat: true; compact: true; text: "Identical copy: " + modelData.filename + (modelData.cited ? " (cited source)" : ""); onClicked: row.sourceRequested(modelData.app_link) }
        }
    }
}
