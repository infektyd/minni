import Foundation
import Testing

@Suite("ResetClock")
struct ResetClockTests {
    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        calendar.locale = Locale(identifier: "en_US_POSIX")
        return calendar
    }

    @Test func minutesUnderAnHour() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let resets = now.addingTimeInterval(51 * 60)
        #expect(ResetClock.caption(resetsAt: resets, now: now, calendar: calendar) == "Resets in 51 min")
    }

    @Test func hoursUnderHalfDay() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let resets = now.addingTimeInterval(2 * 3600)
        #expect(ResetClock.caption(resetsAt: resets, now: now, calendar: calendar) == "Resets in 2 hr")
    }

    @Test func weekdayWhenFar() {
        let now = Date(timeIntervalSince1970: 1_788_031_320)
        let resets = Date(timeIntervalSince1970: 1_788_184_800)
        #expect(ResetClock.caption(resetsAt: resets, now: now, calendar: calendar) == "Resets Thu 12:00 AM")
    }

    @Test func staleAfterFifteenMinutes() {
        let fetched = Date(timeIntervalSince1970: 100)
        #expect(ResetClock.isStale(fetchedAt: fetched, now: fetched.addingTimeInterval(15 * 60 + 1)))
        #expect(!ResetClock.isStale(fetchedAt: fetched, now: fetched.addingTimeInterval(60)))
    }
}
