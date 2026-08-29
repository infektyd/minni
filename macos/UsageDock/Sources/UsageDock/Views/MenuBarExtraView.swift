import AppKit
import SwiftUI

struct MenuBarExtraView: View {
    @Bindable var store: UsageStore
    @Bindable var settings: AppSettings
    @State private var settingsOpen = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(store.snapshot.providers) { provider in
                HStack {
                    ProviderMark(kind: provider.kind, size: 12)
                    Text(provider.kind.displayName)
                    Spacer()
                    Text(provider.primaryPercentText)
                        .monospacedDigit()
                        .foregroundStyle(Palette.accent(for: provider.kind))
                }
            }
            Divider()
            Button("Refresh") {
                Task { await store.refresh() }
            }
            Button("Settings…") {
                settingsOpen = true
            }
            Button("Quit UsageDock") {
                NSApplication.shared.terminate(nil)
            }
        }
        .padding(8)
        .frame(width: 220)
        .sheet(isPresented: $settingsOpen) {
            SettingsView(settings: settings) {
                Task { await store.refresh() }
            }
        }
    }
}

struct MenuBarLabel: View {
    var snapshot: DockSnapshot

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "circle.lefthalf.filled")
            if let hottest = snapshot.hottest {
                Text("\(hottest.kind.displayName.prefix(1)) \(hottest.primaryPercentText)")
                    .monospacedDigit()
            } else {
                Text("Usage")
            }
        }
    }
}
