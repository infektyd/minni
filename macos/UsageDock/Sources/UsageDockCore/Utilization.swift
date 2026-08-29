import Foundation

public enum Utilization {
    /// Community payloads mix 0...100 percents with occasional 0...1 fractions.
    /// Values in (0, 1) are fractions. 0 and 1 stay 0% and 1%, never 100%.
    public static func normalize(_ raw: Double) -> Double {
        let value: Double
        if raw > 0, raw < 1 {
            value = raw * 100
        } else {
            value = raw
        }
        return min(100, max(0, value))
    }

    public static func percentText(_ percent: Double) -> String {
        "\(Int(normalize(percent).rounded()))%"
    }

    public static func usedText(_ percent: Double) -> String {
        "\(Int(normalize(percent).rounded()))% Used"
    }
}
