import Foundation

/// Wire shape for `GET https://api.anthropic.com/api/oauth/usage`.
/// Legacy top-level buckets and the newer `limits` array both appear in the wild.
public struct ClaudeUsagePayload: Sendable, Equatable, Codable {
    public var fiveHour: RawWindow?
    public var sevenDay: RawWindow?
    public var sevenDayOpus: RawWindow?
    public var sevenDaySonnet: RawWindow?
    public var extraUsage: ExtraUsage?
    public var limits: [LimitEntry]

    public struct RawWindow: Sendable, Equatable, Codable {
        public var utilization: Double?
        public var resetsAt: Date?

        enum CodingKeys: String, CodingKey {
            case utilization
            case resetsAt = "resets_at"
        }
    }

    public struct ExtraUsage: Sendable, Equatable, Codable {
        public var isEnabled: Bool?
        public var monthlyLimit: Double?
        public var usedCredits: Double?
        public var utilization: Double?

        enum CodingKeys: String, CodingKey {
            case isEnabled = "is_enabled"
            case monthlyLimit = "monthly_limit"
            case usedCredits = "used_credits"
            case utilization
        }
    }

    public struct LimitEntry: Sendable, Equatable, Codable {
        public var kind: String?
        public var percent: Double?
        public var resetsAt: Date?
        public var scope: Scope?

        enum CodingKeys: String, CodingKey {
            case kind
            case percent
            case resetsAt = "resets_at"
            case scope
        }

        public struct Scope: Sendable, Equatable, Codable {
            public var model: Model?

            public struct Model: Sendable, Equatable, Codable {
                public var displayName: String?

                enum CodingKeys: String, CodingKey {
                    case displayName = "display_name"
                }
            }
        }
    }

    enum CodingKeys: String, CodingKey {
        case fiveHour = "five_hour"
        case sevenDay = "seven_day"
        case sevenDayOpus = "seven_day_opus"
        case sevenDaySonnet = "seven_day_sonnet"
        case extraUsage = "extra_usage"
        case limits
    }

    public init(
        fiveHour: RawWindow? = nil,
        sevenDay: RawWindow? = nil,
        sevenDayOpus: RawWindow? = nil,
        sevenDaySonnet: RawWindow? = nil,
        extraUsage: ExtraUsage? = nil,
        limits: [LimitEntry] = []
    ) {
        self.fiveHour = fiveHour
        self.sevenDay = sevenDay
        self.sevenDayOpus = sevenDayOpus
        self.sevenDaySonnet = sevenDaySonnet
        self.extraUsage = extraUsage
        self.limits = limits
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        fiveHour = try container.decodeIfPresent(RawWindow.self, forKey: .fiveHour)
        sevenDay = try container.decodeIfPresent(RawWindow.self, forKey: .sevenDay)
        sevenDayOpus = try container.decodeIfPresent(RawWindow.self, forKey: .sevenDayOpus)
        sevenDaySonnet = try container.decodeIfPresent(RawWindow.self, forKey: .sevenDaySonnet)
        extraUsage = try container.decodeIfPresent(ExtraUsage.self, forKey: .extraUsage)
        limits = try container.decodeIfPresent([LimitEntry].self, forKey: .limits) ?? []
    }

    public func windows() -> [UsageWindow] {
        var result: [UsageWindow] = []

        if let session = firstLimit("session").flatMap(Self.window(fromLimit:))
            ?? Self.window(from: fiveHour, label: "Current session")
        {
            result.append(UsageWindow(label: "Current session", percent: session.percent, resetsAt: session.resetsAt))
        }

        if let weekly = firstLimit("weekly_all").flatMap(Self.window(fromLimit:))
            ?? Self.window(from: sevenDay, label: "All models")
        {
            result.append(UsageWindow(label: "All models", percent: weekly.percent, resetsAt: weekly.resetsAt))
        }

        let scoped = limits.compactMap { entry -> UsageWindow? in
            guard entry.kind == "weekly_scoped" else { return nil }
            guard let name = entry.scope?.model?.displayName, let percent = entry.percent else {
                return nil
            }
            return UsageWindow(label: name, percent: percent, resetsAt: entry.resetsAt)
        }
        if scoped.isEmpty {
            if let opus = Self.window(from: sevenDayOpus, label: "Opus") {
                result.append(opus)
            }
            if let sonnet = Self.window(from: sevenDaySonnet, label: "Sonnet") {
                result.append(sonnet)
            }
        } else {
            result.append(contentsOf: scoped)
        }

        if extraUsage?.isEnabled == true, let percent = extraUsage?.utilization {
            result.append(UsageWindow(label: "Extra credits", percent: percent, resetsAt: nil))
        }

        return result
    }

    public func snapshot(fetchedAt: Date = Date()) -> ProviderSnapshot {
        let mapped = windows()
        guard !mapped.isEmpty else {
            return .unavailable(.claude, reason: "Usage payload had no windows")
        }
        return ProviderSnapshot(
            kind: .claude,
            status: .live,
            windows: mapped,
            fetchedAt: fetchedAt
        )
    }

    private func firstLimit(_ kind: String) -> LimitEntry? {
        limits.first { $0.kind == kind }
    }

    private static func window(from raw: RawWindow?, label: String) -> UsageWindow? {
        guard let raw, let utilization = raw.utilization else { return nil }
        return UsageWindow(label: label, percent: utilization, resetsAt: raw.resetsAt)
    }

    private static func window(fromLimit entry: LimitEntry) -> UsageWindow? {
        guard let percent = entry.percent else { return nil }
        return UsageWindow(label: entry.kind ?? "Window", percent: percent, resetsAt: entry.resetsAt)
    }
}

public enum ClaudeUsageDecoding {
    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            if let string = try? container.decode(String.self) {
                if let date = withFraction.date(from: string) ?? plain.date(from: string) {
                    return date
                }
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "Bad date: \(string)")
            }
            let timestamp = try container.decode(Double.self)
            if timestamp >= 1e11 {
                return Date(timeIntervalSince1970: timestamp / 1000)
            }
            return Date(timeIntervalSince1970: timestamp)
        }
        return decoder
    }

    public static func decode(_ data: Data) throws -> ClaudeUsagePayload {
        try makeDecoder().decode(ClaudeUsagePayload.self, from: data)
    }
}
