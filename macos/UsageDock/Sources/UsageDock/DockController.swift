import AppKit
import SwiftUI

@MainActor
final class DockController {
    private let store: UsageStore
    private let settings: AppSettings
    private var panel: NSPanel?
    private var observers: [NSObjectProtocol] = []

    init(store: UsageStore, settings: AppSettings) {
        self.store = store
        self.settings = settings
    }

    func start() {
        let panel = makePanel()
        self.panel = panel
        reposition()
        panel.orderFrontRegardless()

        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.reposition() }
        })
        observers.append(center.addObserver(forName: NSWindow.didEnterFullScreenNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.panel?.orderOut(nil) }
        })
        observers.append(center.addObserver(forName: NSWindow.didExitFullScreenNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                self?.reposition()
                self?.panel?.orderFrontRegardless()
            }
        })
    }

    func stop() {
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        observers.removeAll()
        panel?.orderOut(nil)
        panel = nil
    }

    func reposition() {
        guard let panel, let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let visible = screen.visibleFrame
        let height = railHeight
        let width = DockMetrics.railWidth + DockMetrics.popoverWidth + DockMetrics.popoverGap + 24
        let x: CGFloat
        if settings.trailingEdge {
            x = visible.maxX - width - DockMetrics.railInset
        } else {
            x = visible.minX + DockMetrics.railInset
        }
        let y = visible.midY - height / 2
        panel.setFrame(NSRect(x: x, y: y, width: width, height: height), display: true)
    }

    private var railHeight: CGFloat {
        let count = CGFloat(max(1, store.snapshot.providers.count))
        let rings = count * (DockMetrics.ringSize + DockMetrics.ringLabelGap + 14)
        let gaps = max(0, count - 1) * DockMetrics.providerSpacing
        return rings + gaps + DockMetrics.railPadding * 2 + 36
    }

    private func makePanel() -> NSPanel {
        let panel = DockPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 320),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .statusBar
        panel.hidesOnDeactivate = false
        // Stay on desktop spaces only. fullScreenAuxiliary would pin the rail
        // over Keynote / a fullscreen editor, which DESIGN.md forbids.
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary]
        panel.isMovableByWindowBackground = false
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.animationBehavior = .utilityWindow
        let view = EdgeDockView(store: store, settings: settings)
        panel.contentView = NSHostingView(rootView: view)
        return panel
    }
}

final class DockPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}
