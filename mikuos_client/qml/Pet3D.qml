// Real-time 3D Miku for the desktop overlay. Python drives the properties every frame.
import QtQuick
import QtQuick3D
import "../../assets/miku3d/qml" as Asset

Item {
    id: root
    // --- driven from Python -------------------------------------------------
    property string animState: "idle"
    property real yaw: 0              // degrees, 0 = facing the user
    property real modelPx: 220        // on-screen height of standing Miku (px)
    property real footY: 275          // foot line, px from the top of the window
    property real drop: 0             // scene units to lower the model (sitting on an edge)
    property real eyeClose: 0
    property real eyeHappy: 0
    property real eyeSad: 0
    property real mouthSad: 0
    property bool economy: false
    property real mouthOpen: 0        // lip sync / talking
    property real mouthHappy: 0
    property real lookX: 0            // -1..1 gaze
    property real lookY: 0
    property color dotColor: "#e74c3c"
    property bool observing: false
    property bool estop: false

    View3D {
        id: view
        anchors.fill: parent
        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Transparent
            clearColor: "transparent"
            antialiasingMode: root.economy ? SceneEnvironment.NoAA : SceneEnvironment.MSAA
            antialiasingQuality: SceneEnvironment.High
        }
        OrthographicCamera {
            // 1 scene unit (= Miku's height) maps to modelPx pixels; scene y = 0 is the foot line
            horizontalMagnification: root.modelPx
            verticalMagnification: root.modelPx
            y: (root.footY - root.height / 2) / root.modelPx
            z: 10
            clipNear: 0.1
            clipFar: 100
        }
        Asset.Miku {
            animState: root.animState
            eulerRotation.y: root.yaw
            y: -root.drop
            eye_close: root.eyeClose
            eye_happy: root.eyeHappy
            eye_sad: root.eyeSad
            mouth_sad: root.mouthSad
            eyeblow_sad: root.eyeSad * 0.7
            eyeblow_happy: root.eyeHappy * 0.7
            mouth_a: root.mouthOpen
            mouth_happy: root.mouthHappy
            look_left: Math.max(0, root.lookX)
            look_right: Math.max(0, -root.lookX)
            look_up: Math.max(0, -root.lookY)
            look_down: Math.max(0, root.lookY)
        }
    }

    // connection status dot and visible "observation is on" indicator
    Rectangle {
        x: root.width - 16; y: 4; width: 11; height: 11; radius: 5.5
        color: root.estop ? "#8e44ad" : root.dotColor
        border.color: "#55000000"; border.width: 1
    }
    Rectangle {
        visible: root.observing && !root.estop
        x: root.width - 34; y: 5; width: 15; height: 9; radius: 4.5
        color: "#e6ffffff"; border.color: "#1f8f88"; border.width: 1.5
        Rectangle { anchors.centerIn: parent; width: 5; height: 5; radius: 2.5; color: "#1f8f88" }
    }
    Text {
        visible: root.estop
        x: 4; y: 2; text: "STOP"; color: "#8e44ad"; font.bold: true; font.pixelSize: 11
    }
}
