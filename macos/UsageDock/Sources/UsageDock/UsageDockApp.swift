import AppKit
import SwiftUI

@main
struct UsageDockApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            MenuBarExtraView(store: delegate.store, settings: delegate.settings)
        } label: {
            MenuBarLabel(snapshot: delegate.store.snapshot)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(settings: delegate.settings) {
                Task { await delegate.store.refresh() }
                delegate.dock?.reposition()
            }
        }

        Window("UsageDock Preview", id: "preview") {
            ZStack {
                MeshBackdrop()
                HStack {
                    Spacer()
                    EdgeDockView(store: delegate.store, settings: delegate.settings)
                        .padding(.trailing, 24)
                }
            }
            .frame(minWidth: 720, minHeight: 480)
        }
        .defaultSize(width: 900, height: 560)
        .windowResizability(.contentMinSize)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let settings: AppSettings
    let store: UsageStore
    var dock: DockController?

    override init() {
        let settings = AppSettings()
        self.settings = settings
        self.store = UsageStore(settings: settings)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        let controller = DockController(store: store, settings: settings)
        controller.start()
        store.start()
        dock = controller
    }

    func applicationWillTerminate(_ notification: Notification) {
        store.stop()
        dock?.stop()
    }
}

/// In-app preview window so you can iterate without hunting for the rail.
struct MeshBackdrop: View {
    var body: some View {
        LinearGradient(
            colors: [
                Color(red: 0.86, green: 0.88, blue: 0.92),
                Color(red: 0.93, green: 0.82, blue: 0.80),
                Color(red: 0.78, green: 0.86, blue: 0.94),
            ],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
        .ignoresSafeArea()
    }
}

#if DEBUG
#Preview("Rail + popover") {
    let settings = AppSettings()
    let store = UsageStore(settings: settings)
    ZStack {
        MeshBackdrop()
        HStack {
            Spacer()
            EdgeDockView(store: store, settings: settings)
                .padding()
        }
    }
    .frame(width: 720, height: 480)
}
#endif
