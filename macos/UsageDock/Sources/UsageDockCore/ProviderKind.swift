import Foundation

/// Stable provider id. Raw values are settings keys and must not change.
public enum ProviderKind: String, CaseIterable, Sendable, Codable, Identifiable {
    case claude
    case chatgpt
    case perplexity
    case cursor

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .claude: "Claude"
        case .chatgpt: "ChatGPT"
        case .perplexity: "Perplexity"
        case .cursor: "Cursor"
        }
    }

    /// First-run rail matches the three-ring post. Cursor is opt-in.
    public static var defaultEnabled: [ProviderKind] {
        [.claude, .chatgpt, .perplexity]
    }
}
