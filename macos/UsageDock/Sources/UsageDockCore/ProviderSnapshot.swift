import Foundation

public enum SnapshotStatus: Sendable, Equatable, Hashable, Codable {
    case live
    case stale
    case unavailable
    case unsupported
}

public struct ProviderSnapshot: Sendable, Equatable, Hashable, Identifiable, Codable {
    public var kind: ProviderKind
    public var status: SnapshotStatus
    public var windows: [UsageWindow]
    public var fetchedAt: Date?
    public var reason: String?

    public var id: ProviderKind { kind }

    public init(
        kind: ProviderKind,
        status: SnapshotStatus,
        windows: [UsageWindow] = [],
        fetchedAt: Date? = nil,
        reason: String? = nil
    ) {
        self.kind = kind
        self.status = status
        self.windows = windows
        self.fetchedAt = fetchedAt
        self.reason = reason
    }

    public var primaryPercent: Double? {
        windows.first.map(\.percent)
    }

    public var primaryPercentText: String {
        guard let primaryPercent else { return "—" }
        return Utilization.percentText(primaryPercent)
    }

    public var isActionable: Bool {
        status == .live || status == .stale
    }

    public static func unsupported(_ kind: ProviderKind, reason: String) -> ProviderSnapshot {
        ProviderSnapshot(kind: kind, status: .unsupported, reason: reason)
    }

    public static func unavailable(_ kind: ProviderKind, reason: String) -> ProviderSnapshot {
        ProviderSnapshot(kind: kind, status: .unavailable, reason: reason)
    }
}

public struct DockSnapshot: Sendable, Equatable, Codable {
    public var providers: [ProviderSnapshot]
    public var mode: DataMode
    public var generatedAt: Date

    public init(providers: [ProviderSnapshot], mode: DataMode, generatedAt: Date = Date()) {
        self.providers = providers
        self.mode = mode
        self.generatedAt = generatedAt
    }

    public var hottest: ProviderSnapshot? {
        providers
            .filter(\.isActionable)
            .max { lhs, rhs in
                (lhs.primaryPercent ?? -1) < (rhs.primaryPercent ?? -1)
            }
    }
}

public enum DataMode: String, Sendable, Codable {
    case demo
    case live
}
