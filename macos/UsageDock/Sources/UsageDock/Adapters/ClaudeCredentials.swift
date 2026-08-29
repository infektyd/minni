import Darwin
import Foundation

enum ClaudeCredentialsError: Error, Equatable, LocalizedError {
    case notFound
    case apiKeyMode
    case expired(Date)
    case refreshFailed(String)

    var errorDescription: String? {
        switch self {
        case .notFound:
            "Claude Code credentials not found. Run `claude` and sign in."
        case .apiKeyMode:
            "This machine is on an API key, not a Claude Code OAuth login. Subscription % is unavailable."
        case .expired(let date):
            "OAuth token expired at \(date.formatted()). Run `claude` to refresh."
        case .refreshFailed(let reason):
            "OAuth refresh failed (\(reason)). Run `claude` to sign in again."
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

    /// Merge a token-endpoint response without dropping Claude Code's extra oauth keys.
    static func applyRefresh(
        to root: [String: Any],
        accessToken: String,
        refreshToken: String?,
        expiresIn: Int,
        now: Date = Date()
    ) throws -> [String: Any] {
        guard var oauth = root["claudeAiOauth"] as? [String: Any] else {
            throw ClaudeCredentialsError.apiKeyMode
        }
        let previousExpiry = (oauth["expiresAt"] as? Double) ?? (oauth["expiresAt"] as? Int).map(Double.init)
        let milliseconds = previousExpiry.map { $0 >= 1e11 } ?? true
        oauth["accessToken"] = accessToken
        if let refreshToken, !refreshToken.isEmpty {
            oauth["refreshToken"] = refreshToken
        }
        let expiry = now.addingTimeInterval(TimeInterval(expiresIn))
        oauth["expiresAt"] = milliseconds
            ? expiry.timeIntervalSince1970 * 1000
            : expiry.timeIntervalSince1970
        var next = root
        next["claudeAiOauth"] = oauth
        return next
    }

    static func refreshPersistedToken(
        fileURL: URL = fileURL,
        lockURL: URL = lockURL,
        session: URLSession = .shared
    ) async throws -> ClaudeOAuthTokens {
        try await withLock(lockURL) {
            let tokens = try loadIgnoringExpiry(fileURL: fileURL)
            guard let refreshToken = tokens.refreshToken, !refreshToken.isEmpty else {
                throw ClaudeCredentialsError.refreshFailed("no refresh token")
            }
            let refreshed = try await requestRefresh(refreshToken: refreshToken, session: session)
            let data = try Data(contentsOf: fileURL)
            guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw ClaudeCredentialsError.notFound
            }
            let merged = try applyRefresh(
                to: root,
                accessToken: refreshed.accessToken,
                refreshToken: refreshed.refreshToken,
                expiresIn: refreshed.expiresIn
            )
            try writeAtomically(merged, to: fileURL)
            return try loadAccessToken(environment: [:], fileURL: fileURL)
        }
    }

    private static func loadIgnoringExpiry(fileURL: URL) throws -> ClaudeOAuthTokens {
        let farFuture = Date(timeIntervalSince1970: 4_000_000_000)
        return try loadAccessToken(environment: [:], fileURL: fileURL, now: farFuture)
    }

    private struct RefreshResponse: Decodable {
        var accessToken: String
        var refreshToken: String?
        var expiresIn: Int

        enum CodingKeys: String, CodingKey {
            case accessToken = "access_token"
            case refreshToken = "refresh_token"
            case expiresIn = "expires_in"
        }
    }

    private static func requestRefresh(refreshToken: String, session: URLSession) async throws -> RefreshResponse {
        var request = URLRequest(url: URL(string: "https://console.anthropic.com/v1/oauth/token")!)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(ClaudeUsageClient.betaHeader, forHTTPHeaderField: "anthropic-beta")
        // Public Claude Code OAuth client. Required by the token endpoint.
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "grant_type": "refresh_token",
            "refresh_token": refreshToken,
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        ])
        request.timeoutInterval = 15
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw ClaudeCredentialsError.refreshFailed("no response")
        }
        guard http.statusCode == 200 else {
            throw ClaudeCredentialsError.refreshFailed("HTTP \(http.statusCode)")
        }
        do {
            return try JSONDecoder().decode(RefreshResponse.self, from: data)
        } catch {
            throw ClaudeCredentialsError.refreshFailed("unexpected response")
        }
    }

    private static func writeAtomically(_ root: [String: Any], to fileURL: URL) throws {
        let data = try JSONSerialization.data(withJSONObject: root, options: [.prettyPrinted, .sortedKeys])
        let temp = fileURL.appendingPathExtension("tmp")
        try data.write(to: temp, options: .atomic)
        let handle = try FileHandle(forUpdating: temp)
        try handle.synchronize()
        try handle.close()
        _ = try FileManager.default.replaceItemAt(fileURL, withItemAt: temp)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
    }

    private static func withLock<T>(_ lockURL: URL, _ body: () async throws -> T) async throws -> T {
        let directory = lockURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: lockURL.path) {
            FileManager.default.createFile(atPath: lockURL.path, contents: Data())
        }
        let handle = try FileHandle(forUpdating: lockURL)
        let locked = flock(handle.fileDescriptor, LOCK_EX)
        defer {
            _ = flock(handle.fileDescriptor, LOCK_UN)
            try? handle.close()
        }
        guard locked == 0 else {
            throw ClaudeCredentialsError.refreshFailed("could not lock credentials sidecar")
        }
        return try await body()
    }
}
