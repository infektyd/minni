import Foundation
import Testing

@Suite("ClaudeCredentials")
struct ClaudeCredentialsTests {
    @Test func envTokenWins() throws {
        let tokens = try ClaudeCredentials.loadAccessToken(
            environment: ["CLAUDE_CODE_OAUTH_TOKEN": "sk-from-env"],
            fileURL: URL(fileURLWithPath: "/tmp/usage-dock-missing.json")
        )
        #expect(tokens.accessToken == "sk-from-env")
    }

    @Test func epochMilliseconds() {
        let parsed = ClaudeCredentials.date(fromEpoch: 1_893_456_000_000)
        #expect(parsed.milliseconds)
        #expect(parsed.date.timeIntervalSince1970 == 1_893_456_000)
    }

    @Test func epochSeconds() {
        let parsed = ClaudeCredentials.date(fromEpoch: 1_893_456_000)
        #expect(!parsed.milliseconds)
        #expect(parsed.date.timeIntervalSince1970 == 1_893_456_000)
    }

    @Test func missingFile() {
        #expect(throws: ClaudeCredentialsError.notFound) {
            _ = try ClaudeCredentials.loadAccessToken(
                environment: [:],
                fileURL: URL(fileURLWithPath: "/tmp/usage-dock-does-not-exist.json")
            )
        }
    }

    @Test func refreshKeepsExtraOAuthKeysAndMillisecondExpiry() throws {
        let root: [String: Any] = [
            "claudeAiOauth": [
                "accessToken": "old",
                "refreshToken": "old-refresh",
                "expiresAt": 1_893_456_000_000.0,
                "scopes": ["user:inference", "user:profile"],
                "subscriptionType": "max",
            ] as [String: Any],
        ]
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let merged = try ClaudeCredentials.applyRefresh(
            to: root,
            accessToken: "new",
            refreshToken: "rotated",
            expiresIn: 3600,
            now: now
        )
        let oauth = try #require(merged["claudeAiOauth"] as? [String: Any])
        #expect(oauth["accessToken"] as? String == "new")
        #expect(oauth["refreshToken"] as? String == "rotated")
        #expect(oauth["subscriptionType"] as? String == "max")
        #expect(oauth["scopes"] as? [String] == ["user:inference", "user:profile"])
        let expiry = try #require(oauth["expiresAt"] as? Double)
        #expect(expiry == 1_800_003_600_000)
    }
}
