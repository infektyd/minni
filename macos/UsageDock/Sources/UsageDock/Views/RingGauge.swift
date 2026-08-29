import SwiftUI

struct RingGauge: View {
    var snapshot: ProviderSnapshot
    var selected: Bool

    var body: some View {
        VStack(spacing: DockMetrics.ringLabelGap) {
            ZStack {
                Circle()
                    .stroke(Palette.track, lineWidth: DockMetrics.ringStroke)
                Circle()
                    .trim(from: 0, to: ringProgress)
                    .stroke(
                        Palette.accent(for: snapshot.kind),
                        style: StrokeStyle(lineWidth: DockMetrics.ringStroke, lineCap: .round)
                    )
                    .rotationEffect(.degrees(-90))
                ProviderMark(kind: snapshot.kind, size: 20)
                    .opacity(snapshot.isActionable ? 1 : 0.35)
            }
            .frame(width: DockMetrics.ringSize, height: DockMetrics.ringSize)

            Text(snapshot.primaryPercentText)
                .font(.system(size: 11, weight: .semibold, design: .rounded))
                .foregroundStyle(snapshot.isActionable ? Palette.bone : Palette.secondary)
                .monospacedDigit()
        }
        .padding(.vertical, 2)
        .opacity(selected ? 1 : 0.92)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(snapshot.kind.displayName) \(snapshot.primaryPercentText)")
    }

    private var ringProgress: CGFloat {
        guard let percent = snapshot.primaryPercent else { return 0 }
        return CGFloat(percent / 100)
    }
}
