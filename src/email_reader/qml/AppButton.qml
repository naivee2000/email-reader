import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as Theme

Button {
    id: control
    property string iconName: ""
    property bool primary: false
    property bool compact: false
    flat: true
    property bool selected: false
    implicitHeight: compact ? 30 : 34
    implicitWidth: Math.max(text.length ? 52 : 32, Math.ceil(labelMetrics.advanceWidth) + leftPadding + rightPadding + (iconName.length ? 18 : 0) + (text.length && iconName.length ? 6 : 0) + 2)
    leftPadding: compact ? 7 : 10
    rightPadding: compact ? 7 : 10
    topPadding: 4
    bottomPadding: 4
    font.pixelSize: Theme.fontSize
    Accessible.name: text || iconName
    TextMetrics { id: labelMetrics; text: control.text; font: control.font }
    background: Rectangle {
        radius: Theme.radius
        color: control.primary ? Theme.accent : (control.selected ? Theme.selection :
               (control.down || control.hovered ? Theme.hover : (control.flat ? "transparent" : Theme.surface)))
        border.width: control.activeFocus ? 2 : (control.flat || control.primary ? 0 : 1)
        border.color: control.activeFocus ? (control.primary ? Theme.surface : Theme.accent) : Theme.controlBorder
    }
    contentItem: RowLayout {
        spacing: control.text.length && control.iconName.length ? 6 : 0
        AppIcon {
            visible: control.iconName.length > 0
            name: control.iconName
            size: 18
            color: control.primary ? Theme.surface : (control.enabled ? Theme.text : Theme.secondary)
        }
        Text {
            visible: control.text.length > 0
            text: control.text
            textFormat: Text.PlainText
            font: control.font
            color: control.primary ? Theme.surface : (control.enabled ? Theme.text : Theme.secondary)
            elide: Text.ElideRight
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }
}
