import SwiftUI

struct UsagePopover: View {
    var snapshot: ProviderSnapshot
    var now: Date = Date()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                ProviderMark(kind: snapshot.kind, size: 16)
                Text("\(snapshot.kind.displayName) Usage")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Palette.bone)
                Spacer(minLength: 8)
                if let first = snapshot.windows.first {
                    Text(ResetClock.caption(resetsAt: first.resetsAt, now: now))
                        .font(.system(size: 11))
                        .foregroundStyle(Palette.secondary)
                }
            }

            if snapshot.windows.isEmpty {
                Text(snapshot.reason ?? "No utilization data")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(Array(snapshot.windows.enumerated()), id: \.offset) { index, window in
                    WindowRow(
                        window: window,
                        accent: index == 0
                            ? Palette.accent(for: snapshot.kind)
                            : Palette.bar(for: window.percent, primary: Palette.verdigris),
                        showReset: index > 0,
                        now: now
                    )
                }
            }

            Text(footer)
                .font(.system(size: 10))
                .foregroundStyle(Palette.secondary)
        }
        .padding(DockMetrics.popoverPadding)
        .frame(width: DockMetrics.popoverWidth, alignment: .leading)
    }

    private var footer: String {
        if snapshot.status == .stale {
            return "Stale · \(ResetClock.lastSync(fetchedAt: snapshot.fetchedAt, now: now))"
        }
        if snapshot.status == .unsupported || snapshot.status == .unavailable {
            return snapshot.reason ?? snapshot.status.rawLabel
        }
        return ResetClock.lastSync(fetchedAt: snapshot.fetchedAt, now: now)
    }
}

private extension SnapshotStatus {
    var rawLabel: String {
        switch self {
        case .live: "Live"
        case .stale: "Stale"
        case .unavailable: "Unavailable"
        case .unsupported: "Unsupported"
        }
    }
}

struct WindowRow: View {
    var window: UsageWindow
    var accent: Color
    var showReset: Bool
    var now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(window.label)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Palette.bone)
                Spacer()
                if showReset {
                    Text(ResetClock.caption(resetsAt: window.resetsAt, now: now))
                        .font(.system(size: 11))
                        .foregroundStyle(Palette.secondary)
                }
            }
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(Palette.track)
                    Capsule()
                        .fill(accent)
                        .frame(width: max(4, proxy.size.width * CGFloat(window.percent / 100)))
                }
            }
            .frame(height: 6)
            Text(window.usedText)
                .font(.system(size: 11))
                .foregroundStyle(Palette.bone.opacity(0.9))
        }
    }
}
