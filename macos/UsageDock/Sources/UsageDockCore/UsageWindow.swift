import Foundation

public struct UsageWindow: Sendable, Equatable, Hashable, Codable {
    public var label: String
    public var percent: Double
    public var resetsAt: Date?

    public init(label: String, percent: Double, resetsAt: Date? = nil) {
        self.label = label
        self.percent = Utilization.normalize(percent)
        self.resetsAt = resetsAt
    }

    public var percentText: String { Utilization.percentText(percent) }
    public var usedText: String { Utilization.usedText(percent) }
}
