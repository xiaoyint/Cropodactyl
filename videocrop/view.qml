// GPU-rendered video/photo surface. Geometry of the "video" and "image" items
// is driven from Python (videocrop/view.py); the crop overlay is a QWidget
// stacked on top.
import QtQuick
import QtMultimedia

Rectangle {
    color: "#151515"

    VideoOutput {
        objectName: "video"
        fillMode: VideoOutput.Stretch
        visible: false
    }

    Image {
        objectName: "image"
        visible: false
        fillMode: Image.Stretch
        autoTransform: true      // apply EXIF orientation like every photo viewer
        asynchronous: true       // decode off the GUI thread
        cache: false
        smooth: true
        mipmap: true
        sourceSize.width: 4096   // decode at most ~4K for the preview (fast)
        sourceSize.height: 4096
    }
}
