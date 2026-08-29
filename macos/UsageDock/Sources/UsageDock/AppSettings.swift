import Foundation
import SwiftUI

@MainActor
@Observable
final class AppSettings {
    private enum Key {
        static let mode = "usageDock.mode"
        static let enabled = "usageDock.enabled"
        static let edge = "usageDock.edge"
        static let poll = "usageDock.pollSeconds"
        static let launchAtLogin = "usageDock.launchAtLogin"
    }

    var mode: DataMode {
        didSet { UserDefaults.standard.set(mode.rawValue, forKey: Key.mode) }
    }

    var enabledKinds: [ProviderKind] {
        didSet { UserDefaults.standard.set(enabledKinds.map(\.rawValue), forKey: Key.enabled) }
    }

    var trailingEdge: Bool {
        didSet { UserDefaults.standard.set(trailingEdge, forKey: Key.edge) }
    }

    var pollSeconds: TimeInterval {
        didSet {
            let floored = max(DockMetrics.minimumPollSeconds, pollSeconds)
            if floored != pollSeconds {
                pollSeconds = floored
                return
            }
            UserDefaults.standard.set(pollSeconds, forKey: Key.poll)
        }
    }

    var launchAtLogin: Bool {
        didSet { UserDefaults.standard.set(launchAtLogin, forKey: Key.launchAtLogin) }
    }

    init(defaults: UserDefaults = .standard) {
        if let raw = defaults.string(forKey: Key.mode), let mode = DataMode(rawValue: raw) {
            self.mode = mode
        } else {
            self.mode = .demo
        }
        if let stored = defaults.stringArray(forKey: Key.enabled) {
            let parsed = stored.compactMap(ProviderKind.init(rawValue:))
            self.enabledKinds = parsed.isEmpty ? ProviderKind.defaultEnabled : parsed
        } else {
            self.enabledKinds = ProviderKind.defaultEnabled
        }
        self.trailingEdge = defaults.object(forKey: Key.edge) as? Bool ?? true
        let poll = defaults.double(forKey: Key.poll)
        self.pollSeconds = poll >= DockMetrics.minimumPollSeconds ? poll : DockMetrics.minimumPollSeconds
        self.launchAtLogin = defaults.bool(forKey: Key.launchAtLogin)
    }
}
