import Foundation

enum ClaudeUsageClientError: Error, Equatable, LocalizedError {
    case http(Int)
    case badResponse

    var errorDescription: String? {
        switch self {
        case .http(401): "Unauthorized. Run `claude` so the OAuth token refreshes."
        case .http(429): "Usage endpoint rate-limited. Backing off."
        case .http(let code): "Usage endpoint HTTP \(code)"
        case .badResponse: "Usage endpoint returned an unreadable body"
        }
    }
}

actor ClaudeUsageClient {
    static let endpoint = URL(string: "https://api.anthropic.com/api/oauth/usage")!
    static let betaHeader = "oauth-2025-04-20"
    static let userAgent = "UsageDock/0.1 (claude-code/2.0.14)"

    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func fetch(accessToken: String) async throws -> ClaudeUsagePayload {
        var request = URLRequest(url: Self.endpoint)
        request.httpMethod = "GET"
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        request.setValue(Self.betaHeader, forHTTPHeaderField: "anthropic-beta")
        request.setValue(Self.userAgent, forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 15

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw ClaudeUsageClientError.badResponse
        }
        guard http.statusCode == 200 else {
            throw ClaudeUsageClientError.http(http.statusCode)
        }
        return try ClaudeUsageDecoding.decode(data)
    }
}
