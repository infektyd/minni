import Foundation

@MainActor
@Observable
final class UsageStore {
    private(set) var snapshot: DockSnapshot
    private(set) var lastError: String?
    private(set) var isRefreshing = false

    private let settings: AppSettings
    private var pollTask: Task<Void, Never>?
    private var lastGood: [ProviderKind: ProviderSnapshot] = [:]
    private var backoffUntil: Date?

    init(settings: AppSettings) {
        self.settings = settings
        self.snapshot = FixtureCatalog.demo()
    }

    func start() {
        pollTask?.cancel()
        Task { await refresh() }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let interval = UInt64(self.settings.pollSeconds * 1_000_000_000)
                try? await Task.sleep(nanoseconds: interval)
                await self.refresh()
            }
        }
    }

    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    func refresh() async {
        if let backoffUntil, backoffUntil > Date() {
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }

        let enabled = settings.enabledKinds
        let now = Date()

        if settings.mode == .demo {
            snapshot = filtered(FixtureCatalog.demo(now: now), enabled: enabled)
            lastError = nil
            return
        }

        var next: [ProviderSnapshot] = []
        for adapter in AdapterCatalog.liveAdapters() where enabled.contains(adapter.kind) {
            let fetched = await adapter.fetch()
            let resolved = merge(fetched, now: now)
            next.append(resolved)
            if fetched.status == .unavailable, fetched.reason?.contains("429") == true {
                backoffUntil = now.addingTimeInterval(5 * 60)
            }
            if fetched.isActionable {
                lastGood[fetched.kind] = fetched
            }
        }
        snapshot = DockSnapshot(providers: next, mode: .live, generatedAt: now)
        lastError = next.first(where: { $0.status == .unavailable })?.reason
    }

    private func merge(_ fetched: ProviderSnapshot, now: Date) -> ProviderSnapshot {
        if fetched.isActionable {
            return fetched
        }
        guard var previous = lastGood[fetched.kind], let fetchedAt = previous.fetchedAt else {
            return fetched
        }
        previous.status = ResetClock.isStale(fetchedAt: fetchedAt, now: now) ? .stale : previous.status
        previous.reason = fetched.reason
        return previous
    }

    private func filtered(_ demo: DockSnapshot, enabled: [ProviderKind]) -> DockSnapshot {
        DockSnapshot(
            providers: demo.providers.filter { enabled.contains($0.kind) },
            mode: .demo,
            generatedAt: demo.generatedAt
        )
    }
}
