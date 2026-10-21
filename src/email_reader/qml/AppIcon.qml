import QtQuick
import "Theme.js" as Theme

Item {
    id: root
    property string name: "file"
    property color color: Theme.secondary
    property int size: 18
    implicitWidth: size
    implicitHeight: size
    readonly property var paths: ({
        inbox: "M3 4h18v16H3z M3 13h5l2 3h4l2-3h5",
        mail: "M3 5h18v14H3z M3 5l9 7 9-7",
        sent: "M3 3l18 9-18 9 4-9z M7 12h14",
        drafts: "M5 3h10l4 4v14H5z M14 3v5h5 M8 12h8 M8 16h6",
        important: "M12 3l2.8 5.8 6.4.9-4.6 4.5 1.1 6.4-5.7-3-5.7 3 1.1-6.4L2.8 9.7l6.4-.9z",
        search: "M17 17l4 4 M19 10a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
        refresh: "M20 8a8 8 0 1 0 0 8 M20 3v5h-5",
        settings: "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
        compose: "M4 16L16 4l4 4L8 20H4z M13 7l4 4",
        close: "M6 6l12 12 M18 6L6 18",
        back: "M14 5l-7 7 7 7 M7 12h14",
        forward: "M10 5l7 7-7 7 M3 12h14",
        reply: "M10 5l-7 7 7 7 M3 12h11a7 7 0 0 1 7 7",
        replyAll: "M8 5l-6 7 6 7 M14 5l-6 7 6 7 M8 12h7a6 6 0 0 1 6 6",
        more: "M5 11v2 M12 11v2 M19 11v2",
        chevron: "M6 9l6 6 6-6",
        "chevron-down": "M6 9l6 6 6-6",
        "chevron-right": "M9 6l6 6-6 6",
        external: "M14 3h7v7 M21 3L10 14 M10 5H3v16h16v-7",
        attachment: "M8 13l7-7a3 3 0 0 1 4 4L9 20a5 5 0 0 1-7-7L13 2 M6 15l8-8",
        file: "M5 3h10l4 4v14H5z M14 3v5h5",
        export: "M12 16V2 M7 7l5-5 5 5 M4 13v8h16v-8",
        account: "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-2a8 8 0 0 1 16 0v2",
        history: "M4 7a9 9 0 1 1-1 8 M3 3v5h5 M12 6v6l4 2",
        info: "M12 10v7 M12 6v1 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0"
    })
    Image {
    anchors.fill: parent
    sourceSize.width: root.size * 2
    sourceSize.height: root.size * 2
    fillMode: Image.PreserveAspectFit
    source: "data:image/svg+xml;utf8," + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="' +
        (root.paths[root.name] || root.paths.file) + '" fill="none" stroke="' + root.color +
        '" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    }
}
