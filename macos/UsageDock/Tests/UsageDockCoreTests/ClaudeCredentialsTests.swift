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
}
