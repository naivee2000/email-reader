import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

ColumnLayout {
    id: root
    property alias text: field.text
    property string accountId: ""
    property string label: "To"
    property string fieldName: ""
    property var suggestions: []
    readonly property string suggestionToken: fieldName + "-" + Math.random().toString(36).slice(2)
    property string pendingToken: ""
    property string pendingQuery: ""
    spacing: 3

    function dismissSuggestions() {
        if (!field.activeFocus || !root.suggestions.length) return false
        suggestionTimer.stop()
        pendingToken = ""
        root.suggestions = []
        return true
    }

    function choose(index) {
        if (index < 0 || index >= suggestions.length) return
        const parts = field.text.split(/[,;]/)
        parts[parts.length - 1] = suggestions[index].email
        field.text = parts.map(part => part.trim()).filter(part => part.length).join(", ") + ", "
        suggestions = []
        suggestionTimer.stop()
        pendingToken = ""
        field.forceActiveFocus()
        field.cursorPosition = field.text.length
    }
    function suggest() {
        const parts = field.text.split(/[,;]/)
        pendingQuery = parts[parts.length - 1].trim()
        pendingToken = suggestionToken
        suggestionTimer.restart()
    }
    onAccountIdChanged: { suggestions = []; if (field.activeFocus) suggest() }
    Timer {
        id: suggestionTimer
        interval: 150
        onTriggered: if (field.activeFocus) App.requestRecipientSuggestions(root.accountId, root.pendingQuery, root.pendingToken)
    }
    Connections {
        target: App
        function onRecipientSuggestionsReady(token, accountId, query, results) {
            if (token !== root.pendingToken || accountId !== root.accountId || query !== root.pendingQuery || !field.activeFocus) return
            root.suggestions = results
            matches.currentIndex = 0
        }
    }
    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Label { text: root.label; color: Theme.secondary; font.pixelSize: 14; Layout.preferredWidth: 54 }
        TextField {
            id: field
            objectName: root.fieldName
            Layout.fillWidth: true
            implicitHeight: 34
            font.pixelSize: 14
            selectByMouse: true
            Accessible.name: root.label + " recipients"
            color: Theme.text; placeholderTextColor: Theme.secondary
            selectedTextColor: Theme.text; selectionColor: Theme.selection
            placeholderText: "Name or email address"
            background: Rectangle { color: Theme.surface; radius: 6; border.color: field.activeFocus ? Theme.accent : Theme.controlBorder }
            onTextEdited: root.suggest()
            onActiveFocusChanged: {
                if (activeFocus) root.suggest()
                else { suggestionTimer.stop(); root.pendingToken = ""; root.suggestions = [] }
            }
            Keys.onDownPressed: matches.currentIndex = Math.min(root.suggestions.length - 1, matches.currentIndex + 1)
            Keys.onUpPressed: matches.currentIndex = Math.max(0, matches.currentIndex - 1)
            Keys.onReturnPressed: root.choose(matches.currentIndex)
            Keys.onEnterPressed: root.choose(matches.currentIndex)
            Keys.onEscapePressed: function(event) {
                event.accepted = root.dismissSuggestions()
            }
            Keys.onTabPressed: function(event) {
                if (root.suggestions.length) root.choose(matches.currentIndex)
                else event.accepted = false
            }
        }
    }
    ListView {
        id: matches
        Layout.fillWidth: true
        Layout.leftMargin: 62
        Layout.preferredHeight: visible ? Math.min(108, contentHeight) : 0
        visible: field.activeFocus && root.suggestions.length > 0
        clip: true
        model: root.suggestions
        delegate: ItemDelegate {
            required property var modelData
            required property int index
            objectName: root.fieldName + "-suggestion-" + index
            text: modelData.label
            width: matches.width; height: 36
            focusPolicy: Qt.NoFocus
            background: Rectangle { radius: 6; color: matches.currentIndex === index ? Theme.selection : Theme.surface }
            contentItem: Text { text: modelData.label; color: Theme.text; font.pixelSize: 14; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
            onClicked: root.choose(index)
        }
    }
}
