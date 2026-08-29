import Foundation
import Testing

@Suite("ClaudeUsagePayload")
struct ClaudeUsagePayloadTests {
    private var fixtures: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures")
    }

    @Test func legacyBuckets() throws {
        let data = try Data(contentsOf: fixtures.appendingPathComponent("claude-usage-legacy.json"))
        let payload = try ClaudeUsageDecoding.decode(data)
        let windows = payload.windows()
        #expect(windows.map(\.label) == ["Current session", "All models", "Opus", "Sonnet"])
        #expect(windows[0].percent == 73)
        #expect(windows[1].percent == 7)
    }

    @Test func limitsArrayWinsAndNormalizesFractions() throws {
        let data = try Data(contentsOf: fixtures.appendingPathComponent("claude-usage-limits.json"))
        let payload = try ClaudeUsageDecoding.decode(data)
        let windows = payload.windows()
        #expect(windows.map(\.label) == ["Current session", "All models", "Fable"])
        #expect(windows[0].percent == 73)
        #expect(windows[1].percent == 7)
        #expect(windows[2].percent == 27)
    }

    @Test func emptyPayloadIsUnavailable() {
        let snapshot = ClaudeUsagePayload().snapshot()
        #expect(snapshot.status == .unavailable)
    }
}
