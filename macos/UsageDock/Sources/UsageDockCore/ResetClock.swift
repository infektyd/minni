import Foundation

public enum ResetClock {
    public static let staleAfter: TimeInterval = 15 * 60

    public static func isStale(fetchedAt: Date, now: Date = Date()) -> Bool {
        now.timeIntervalSince(fetchedAt) > staleAfter
    }

    /// "Resets in 51 min" / "Resets in 2 hr" / "Resets Thu 12:00 AM"
    public static func caption(resetsAt: Date?, now: Date = Date(), calendar: Calendar = .current) -> String {
        guard let resetsAt else { return "" }
        let seconds = resetsAt.timeIntervalSince(now)
        if seconds <= 0 {
            return "Resetting"
        }
        if seconds < 60 * 60 {
            let minutes = max(1, Int((seconds / 60).rounded()))
            return "Resets in \(minutes) min"
        }
        if seconds < 60 * 60 * 12 {
            let hours = Int((seconds / 3600).rounded())
            return hours == 1 ? "Resets in 1 hr" : "Resets in \(hours) hr"
        }
        return "Resets \(weekdayTime(resetsAt, calendar: calendar))"
    }

    public static func weekdayTime(_ date: Date, calendar: Calendar = .current) -> String {
        let weekday = calendar.shortWeekdaySymbols[calendar.component(.weekday, from: date) - 1]
        let hour24 = calendar.component(.hour, from: date)
        let minute = calendar.component(.minute, from: date)
        let suffix = hour24 >= 12 ? "PM" : "AM"
        var hour12 = hour24 % 12
        if hour12 == 0 { hour12 = 12 }
        return "\(weekday) \(hour12):\(String(format: "%02d", minute)) \(suffix)"
    }

    public static func lastSync(fetchedAt: Date?, now: Date = Date()) -> String {
        guard let fetchedAt else { return "Never synced" }
        let seconds = now.timeIntervalSince(fetchedAt)
        if seconds < 10 { return "Just now" }
        if seconds < 60 { return "Synced \(Int(seconds))s ago" }
        if seconds < 3600 { return "Synced \(Int(seconds / 60))m ago" }
        return "Synced \(Int(seconds / 3600))h ago"
    }
}
