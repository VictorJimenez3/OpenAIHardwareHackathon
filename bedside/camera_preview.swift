import AppKit
import ImageIO

private struct PreviewStatus: Decodable {
    let cue: String
    let receipt: String
}

private final class CameraImageView: NSView {
    var image: CGImage? {
        didSet { needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(srgbRed: 0.031, green: 0.071, blue: 0.073, alpha: 1).setFill()
        bounds.fill()

        guard let image, let context = NSGraphicsContext.current?.cgContext else { return }
        let scale = min(bounds.width / CGFloat(image.width), bounds.height / CGFloat(image.height))
        let width = CGFloat(image.width) * scale
        let height = CGFloat(image.height) * scale
        context.saveGState()
        context.translateBy(x: bounds.midX + width / 2, y: bounds.midY - height / 2)
        context.scaleBy(x: -1, y: 1)
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        context.restoreGState()
    }
}

private final class PreviewController: NSObject, NSWindowDelegate {
    private let panel: NSPanel
    private let camera = CameraImageView()
    private let waiting = NSTextField(labelWithString: "Waiting for camera frames")
    private let cue = NSTextField(wrappingLabelWithString: "")
    private let receipt = NSTextField(wrappingLabelWithString: "")

    private let updateLock = NSLock()
    private var nextImage: CGImage?
    private var nextStatus: PreviewStatus?
    private var updateQueued = false

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 330),
            styleMask: [.titled, .closable, .resizable, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        super.init()
        panel.title = "Camera preview"
        panel.appearance = NSAppearance(named: .darkAqua)
        panel.delegate = self
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.contentMinSize = NSSize(width: 290, height: 250)

        let content = NSView()
        let status = NSView()
        status.wantsLayer = true
        status.layer?.backgroundColor = NSColor(
            srgbRed: 0.064, green: 0.141, blue: 0.140, alpha: 1
        ).cgColor

        waiting.font = .systemFont(ofSize: 13, weight: .medium)
        waiting.textColor = NSColor(srgbRed: 0.68, green: 0.79, blue: 0.78, alpha: 1)
        cue.font = .systemFont(ofSize: 13, weight: .medium)
        cue.textColor = waiting.textColor
        receipt.font = .systemFont(ofSize: 22, weight: .semibold)
        receipt.textColor = NSColor(srgbRed: 0.71, green: 1, blue: 0.88, alpha: 1)

        for label in [cue, receipt] {
            label.maximumNumberOfLines = 0
            label.setContentCompressionResistancePriority(.required, for: .vertical)
            label.isHidden = true
        }

        let labels = NSStackView(views: [receipt, cue])
        labels.orientation = .vertical
        labels.alignment = .leading
        labels.spacing = 4
        labels.detachesHiddenViews = true

        for view in [camera, waiting, status, labels] {
            view.translatesAutoresizingMaskIntoConstraints = false
        }
        content.addSubview(camera)
        camera.addSubview(waiting)
        content.addSubview(status)
        status.addSubview(labels)
        NSLayoutConstraint.activate([
            camera.topAnchor.constraint(equalTo: content.topAnchor),
            camera.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            camera.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            camera.bottomAnchor.constraint(equalTo: status.topAnchor),
            status.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            status.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            status.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            labels.leadingAnchor.constraint(equalTo: status.leadingAnchor, constant: 16),
            labels.trailingAnchor.constraint(equalTo: status.trailingAnchor, constant: -16),
            labels.topAnchor.constraint(equalTo: status.topAnchor, constant: 11),
            labels.bottomAnchor.constraint(equalTo: status.bottomAnchor, constant: -11),
            waiting.centerXAnchor.constraint(equalTo: camera.centerXAnchor),
            waiting.centerYAnchor.constraint(equalTo: camera.centerYAnchor),
        ])
        panel.contentView = content
    }

    func show() {
        let mouse = NSEvent.mouseLocation
        if let screen = NSScreen.screens.first(where: { $0.frame.contains(mouse) }) ?? NSScreen.main {
            let frame = screen.visibleFrame
            panel.setFrameTopLeftPoint(NSPoint(
                x: max(frame.minX, frame.maxX - panel.frame.width - 24),
                y: frame.maxY - 24
            ))
        }
        panel.orderFrontRegardless()
    }

    func readInput() {
        let decoder = JSONDecoder()
        while let line = readLine() {
            autoreleasepool {
                if line.first == "{", let status = try? decoder.decode(PreviewStatus.self, from: Data(line.utf8)) {
                    enqueue(status: status)
                } else if let data = Data(base64Encoded: line),
                          let source = CGImageSourceCreateWithData(data as CFData, nil),
                          let image = CGImageSourceCreateImageAtIndex(
                              source, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary
                          ) {
                    enqueue(image: image)
                }
            }
        }
        DispatchQueue.main.async { NSApp.terminate(nil) }
    }

    private func enqueue(image: CGImage? = nil, status: PreviewStatus? = nil) {
        updateLock.lock()
        defer { updateLock.unlock() }
        if let image { nextImage = image }
        if let status { nextStatus = status }
        if !updateQueued {
            updateQueued = true
            DispatchQueue.main.async { self.applyUpdate() }
        }
    }

    private func applyUpdate() {
        updateLock.lock()
        let image = nextImage
        let status = nextStatus
        nextImage = nil
        nextStatus = nil
        updateQueued = false
        updateLock.unlock()

        if let image {
            camera.image = image
            waiting.isHidden = true
        }
        if let status {
            cue.stringValue = status.cue
            receipt.stringValue = status.receipt
            cue.isHidden = status.cue.isEmpty
            receipt.isHidden = status.receipt.isEmpty
        }
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
guard !NSScreen.screens.isEmpty else { exit(1) }
private let preview = PreviewController()
DispatchQueue.main.async {
    preview.show()
    FileHandle.standardOutput.write(Data("ready\n".utf8))
    DispatchQueue.global(qos: .userInitiated).async { preview.readInput() }
}
app.run()
