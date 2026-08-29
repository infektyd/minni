import Foundation

struct UnsupportedAdapter: ProviderAdapter {
    let kind: ProviderKind
    let reason: String

    func fetch() async -> ProviderSnapshot {
        .unsupported(kind, reason: reason)
    }
}

enum AdapterCatalog {
    static let chatgptReason = "ChatGPT does not publish a session utilization API"
    static let perplexityReason = "Perplexity does not publish a utilization API"
    static let cursorReason = "Cursor usage is not wired yet. No unofficial scrape."

    static func liveAdapters() -> [any ProviderAdapter] {
        [
            ClaudeAdapter(),
            UnsupportedAdapter(kind: .chatgpt, reason: chatgptReason),
            UnsupportedAdapter(kind: .perplexity, reason: perplexityReason),
            UnsupportedAdapter(kind: .cursor, reason: cursorReason),
        ]
    }
}
