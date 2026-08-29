import ServiceManagement
import SwiftUI

struct SettingsView: View {
    @Bindable var settings: AppSettings
    var onChange: () -> Void
    @State private var loginItemError: String?

    var body: some View {
        Form {
            Section("Data") {
                Picker("Mode", selection: $settings.mode) {
                    Text("Demo").tag(DataMode.demo)
                    Text("Live").tag(DataMode.live)
                }
                .pickerStyle(.segmented)
                Text(settings.mode == .demo
                     ? "Tweet-matching fixtures. Labeled on the rail."
                     : "Claude is fetched. ChatGPT and Perplexity stay empty until a real adapter exists.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Providers") {
                ForEach(ProviderKind.allCases) { kind in
                    Toggle(kind.displayName, isOn: binding(for: kind))
                }
            }

            Section("Rail") {
                Picker("Edge", selection: $settings.trailingEdge) {
                    Text("Trailing").tag(true)
                    Text("Leading").tag(false)
                }
                Stepper(
                    value: $settings.pollSeconds,
                    in: DockMetrics.minimumPollSeconds...3600,
                    step: 30
                ) {
                    Text("Poll every \(Int(settings.pollSeconds))s")
                }
                Toggle("Launch at login", isOn: $settings.launchAtLogin)
                    .onChange(of: settings.launchAtLogin) { _, enabled in
                        updateLoginItem(enabled)
                    }
                if let loginItemError {
                    Text(loginItemError)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 360)
        .padding()
        .onChange(of: settings.mode) { _, _ in onChange() }
        .onChange(of: settings.enabledKinds) { _, _ in onChange() }
    }

    private func binding(for kind: ProviderKind) -> Binding<Bool> {
        Binding(
            get: { settings.enabledKinds.contains(kind) },
            set: { isOn in
                if isOn {
                    if !settings.enabledKinds.contains(kind) {
                        settings.enabledKinds.append(kind)
                    }
                } else if settings.enabledKinds.count > 1 {
                    settings.enabledKinds.removeAll { $0 == kind }
                }
            }
        )
    }

    private func updateLoginItem(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            loginItemError = error.localizedDescription
        }
    }
}
