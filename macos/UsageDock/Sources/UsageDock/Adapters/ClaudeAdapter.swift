import Foundation

struct ClaudeAdapter: ProviderAdapter {
    let kind: ProviderKind = .claude
    let client: ClaudeUsageClient

    init(client: ClaudeUsageClient = ClaudeUsageClient()) {
        self.client = client
    }

    func fetch() async -> ProviderSnapshot {
        do {
            let tokens = try ClaudeCredentials.loadAccessToken()
            let payload = try await client.fetch(accessToken: tokens.accessToken)
            return payload.snapshot(fetchedAt: Date())
        } catch let error as ClaudeCredentialsError {
            return .unavailable(.claude, reason: error.localizedDescription)
        } catch let error as ClaudeUsageClientError {
            return .unavailable(.claude, reason: error.localizedDescription)
        } catch {
            return .unavailable(.claude, reason: error.localizedDescription)
        }
    }
}
