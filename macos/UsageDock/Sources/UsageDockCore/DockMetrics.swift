import Foundation

/// Geometry from DESIGN.md. Views read these instead of scattering magic numbers.
public enum DockMetrics {
    public static let railWidth: CGFloat = 72
    public static let railInset: CGFloat = 8
    public static let railPadding: CGFloat = 14
    public static let ringSize: CGFloat = 44
    public static let ringStroke: CGFloat = 3.5
    public static let ringLabelGap: CGFloat = 6
    public static let providerSpacing: CGFloat = 16
    public static let popoverWidth: CGFloat = 268
    public static let popoverCorner: CGFloat = 16
    public static let popoverPadding: CGFloat = 14
    public static let popoverGap: CGFloat = 10
    public static let arrowWidth: CGFloat = 8
    public static let arrowHeight: CGFloat = 10
    public static let hoverDelayNanoseconds: UInt64 = 220_000_000
    public static let minimumPollSeconds: TimeInterval = 180
}
