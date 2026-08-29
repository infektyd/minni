import SwiftUI

struct EdgeDockView: View {
    @Bindable var store: UsageStore
    @Bindable var settings: AppSettings
    var now: Date = Date()

    @State private var hovered: ProviderKind?
    @State private var pinned: ProviderKind?
    @State private var hoverTask: Task<Void, Never>?

    var body: some View {
        HStack(alignment: .top, spacing: DockMetrics.popoverGap) {
            if settings.trailingEdge {
                popoverColumn
                rail
            } else {
                rail
                popoverColumn
            }
        }
        .padding(2)
        .onExitCommand {
            pinned = nil
            hovered = nil
        }
    }

    private var visible: ProviderSnapshot? {
        let kind = pinned ?? hovered
        return store.snapshot.providers.first { $0.kind == kind }
    }

    @ViewBuilder
    private var popoverColumn: some View {
        if let visible {
            UsagePopover(snapshot: visible, now: now)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: DockMetrics.popoverCorner, style: .continuous))
                .overlay(alignment: settings.trailingEdge ? .trailing : .leading) {
                    PopoverArrow(pointsTrailing: settings.trailingEdge)
                        .fill(Color.black.opacity(0.35))
                        .frame(width: DockMetrics.arrowWidth, height: DockMetrics.arrowHeight)
                        .offset(x: settings.trailingEdge ? DockMetrics.arrowWidth - 1 : -(DockMetrics.arrowWidth - 1))
                }
                .shadow(color: .black.opacity(0.35), radius: 16, y: 6)
                .transition(.opacity.combined(with: .move(edge: settings.trailingEdge ? .trailing : .leading)))
        }
    }

    private var rail: some View {
        VStack(spacing: DockMetrics.providerSpacing) {
            ForEach(store.snapshot.providers) { provider in
                RingGauge(
                    snapshot: provider,
                    selected: (pinned ?? hovered) == provider.kind
                )
                .onHover { inside in
                    handleHover(inside, kind: provider.kind)
                }
                .onTapGesture {
                    pinned = pinned == provider.kind ? nil : provider.kind
                }
            }
            if store.snapshot.mode == .demo {
                Text("DEMO")
                    .font(.system(size: 8, weight: .bold, design: .rounded))
                    .tracking(0.6)
                    .foregroundStyle(Palette.mustard)
            }
        }
        .padding(.vertical, DockMetrics.railPadding)
        .padding(.horizontal, 14)
        .frame(width: DockMetrics.railWidth)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay {
            Capsule().stroke(Color.white.opacity(0.08), lineWidth: 1)
        }
        .shadow(color: .black.opacity(0.4), radius: 12, y: 2)
    }

    private func handleHover(_ inside: Bool, kind: ProviderKind) {
        hoverTask?.cancel()
        if inside {
            hoverTask = Task {
                try? await Task.sleep(nanoseconds: DockMetrics.hoverDelayNanoseconds)
                guard !Task.isCancelled else { return }
                hovered = kind
            }
        } else {
            hovered = nil
        }
    }
}

struct PopoverArrow: Shape {
    var pointsTrailing: Bool

    func path(in rect: CGRect) -> Path {
        var path = Path()
        if pointsTrailing {
            path.move(to: CGPoint(x: rect.minX, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
            path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        } else {
            path.move(to: CGPoint(x: rect.maxX, y: rect.minY))
            path.addLine(to: CGPoint(x: rect.minX, y: rect.midY))
            path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        }
        path.closeSubpath()
        return path
    }
}
