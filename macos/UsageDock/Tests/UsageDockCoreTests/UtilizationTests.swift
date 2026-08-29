import Foundation
import Testing

@Suite("Utilization")
struct UtilizationTests {
    @Test func fractionBecomesPercent() {
        #expect(Utilization.normalize(0.73) == 73)
        #expect(Utilization.percentText(0.73) == "73%")
    }

    @Test func alreadyPercentStays() {
        #expect(Utilization.normalize(73) == 73)
        #expect(Utilization.usedText(7) == "7% Used")
    }

    @Test func zeroAndOneArePercentsNotFractions() {
        #expect(Utilization.normalize(0) == 0)
        #expect(Utilization.normalize(1) == 1)
        #expect(Utilization.percentText(1) == "1%")
    }

    @Test func clampsToClosedUnit() {
        #expect(Utilization.normalize(-4) == 0)
        #expect(Utilization.normalize(140) == 100)
    }
}
