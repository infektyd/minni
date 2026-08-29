import Foundation

/// Tweet-matching numbers for demo mode and layout comparison.
/// These are labeled demo data. Live mode must never return them.
public enum FixtureCatalog {
    public static let claudeSessionReset = Date(timeIntervalSince1970: 1_788_034_920)
    public static let claudeWeeklyReset = Date(timeIntervalSince1970: 1_788_184_800)

    public static func demo(now: Date = Date()) -> DockSnapshot {
        let sessionReset = now.addingTimeInterval(51 * 60)
        let weeklyReset = weeklyThursdayMidnight(after: now)
        return DockSnapshot(
            providers: [
                ProviderSnapshot(
                    kind: .claude,
                    status: .live,
                    windows: [
                        UsageWindow(label: "Current session", percent: 73, resetsAt: sessionReset),
                        UsageWindow(label: "All models", percent: 7, resetsAt: weeklyReset),
                    ],
                    fetchedAt: now
                ),
                ProviderSnapshot(
                    kind: .chatgpt,
                    status: .live,
                    windows: [
                        UsageWindow(label: "Current session", percent: 21, resetsAt: now.addingTimeInterval(3 * 3600)),
                        UsageWindow(label: "Weekly", percent: 14, resetsAt: weeklyReset),
                    ],
                    fetchedAt: now
                ),
                ProviderSnapshot(
                    kind: .perplexity,
                    status: .live,
                    windows: [
                        UsageWindow(label: "Current session", percent: 52, resetsAt: now.addingTimeInterval(2 * 3600)),
                        UsageWindow(label: "Weekly", percent: 31, resetsAt: weeklyReset),
                    ],
                    fetchedAt: now
                ),
            ],
            mode: .demo,
            generatedAt: now
        )
    }

    public static func livePlaceholders() -> [ProviderSnapshot] {
        [
            .unavailable(.claude, reason: "No Claude Code OAuth token yet"),
            .unsupported(.chatgpt, reason: "ChatGPT does not publish a session utilization API"),
            .unsupported(.perplexity, reason: "Perplexity does not publish a utilization API"),
        ]
    }

    public static func weeklyThursdayMidnight(after now: Date, calendar: Calendar = .current) -> Date {
        var calendar = calendar
        calendar.timeZone = .current
        let weekday = calendar.component(.weekday, from: now)
        // Thursday == 5 in Gregorian
        var days = (5 - weekday + 7) % 7
        if days == 0, calendar.component(.hour, from: now) > 0 || calendar.component(.minute, from: now) > 0 {
            days = 7
        }
        let start = calendar.startOfDay(for: now)
        return calendar.date(byAdding: .day, value: days, to: start) ?? now.addingTimeInterval(86400 * 3)
    }
}
