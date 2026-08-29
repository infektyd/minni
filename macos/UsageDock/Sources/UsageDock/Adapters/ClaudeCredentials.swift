import Foundation

enum ClaudeCredentialsError: Error, Equatable, LocalizedError {
    case notFound
    case apiKeyMode
    case expired(Date)

    var errorDescription: String? {
        switch self {
        case .notFound:
            "Claude Code credentials not found. Run `claude` and sign in."
        case .apiKeyMode:
            "This machine is on an API key, not a Claude Code OAuth login. Subscription % is unavailable."
        case .expired(let date):
            "OAuth token expired at \(date.formatted()). Run `claude` to refresh."
        }
    }
}

struct ClaudeOAuthTokens: Sendable, Equatable {
    var accessToken: String
    var refreshToken: String?
    var expiresAt: Date?
    var expiresAtWasMilliseconds: Bool
}

enum ClaudeCredentials {
    static var fileURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".claude/.credentials.json")
    }

    static var lockURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".claude/.credentials.json.lock")
    }

    static func loadAccessToken(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileURL: URL = fileURL,
        now: Date = Date()
    ) throws -> ClaudeOAuthTokens {
        if let env = environment["CLAUDE_CODE_OAUTH_TOKEN"], !env.isEmpty {
            return ClaudeOAuthTokens(accessToken: env, refreshToken: nil, expiresAt: nil, expiresAtWasMilliseconds: true)
        }

        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            throw ClaudeCredentialsError.notFound
        }
        let data = try Data(contentsOf: fileURL)
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ClaudeCredentialsError.notFound
        }
        guard let oauth = root["claudeAiOauth"] as? [String: Any] else {
            throw ClaudeCredentialsError.apiKeyMode
        }
        guard let token = oauth["accessToken"] as? String, !token.isEmpty else {
            throw ClaudeCredentialsError.notFound
        }

        let expiresAt: Date?
        var usedMilliseconds = true
        if let raw = oauth["expiresAt"] as? Double {
            let parsed = Self.date(fromEpoch: raw)
            expiresAt = parsed.date
            usedMilliseconds = parsed.milliseconds
        } else if let raw = oauth["expiresAt"] as? Int {
            let parsed = Self.date(fromEpoch: Double(raw))
            expiresAt = parsed.date
            usedMilliseconds = parsed.milliseconds
        } else {
            expiresAt = nil
        }

        if let expiresAt, expiresAt.timeIntervalSince(now) < 60 {
            throw ClaudeCredentialsError.expired(expiresAt)
        }

        return ClaudeOAuthTokens(
            accessToken: token,
            refreshToken: oauth["refreshToken"] as? String,
            expiresAt: expiresAt,
            expiresAtWasMilliseconds: usedMilliseconds
        )
    }

    static func date(fromEpoch raw: Double) -> (date: Date, milliseconds: Bool) {
        if raw >= 1e11 {
            return (Date(timeIntervalSince1970: raw / 1000), true)
        }
        return (Date(timeIntervalSince1970: raw), false)
    }
}
