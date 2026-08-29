import Foundation

struct ClaudeAdapter: ProviderAdapter {
    let kind: ProviderKind = .claude
    let client: ClaudeUsageClient

    init(client: ClaudeUsageClient = ClaudeUsageClient()) {
        self.client = client
    }

    func fetch() async -> ProviderSnapshot {
        do {
            return try await fetchOnce(allowRefresh: true)
        } catch let error as ClaudeCredentialsError {
            return .unavailable(.claude, reason: error.localizedDescription)
        } catch let error as ClaudeUsageClientError {
            return .unavailable(.claude, reason: error.localizedDescription)
        } catch {
            return .unavailable(.claude, reason: error.localizedDescription)
        }
    }

    private func fetchOnce(allowRefresh: Bool) async throws -> ProviderSnapshot {
        let tokens: ClaudeOAuthTokens
        do {
            tokens = try ClaudeCredentials.loadAccessToken()
        } catch ClaudeCredentialsError.expired {
            tokens = try await ClaudeCredentials.refreshPersistedToken()
        }

        do {
            let payload = try await client.fetch(accessToken: tokens.accessToken)
            return payload.snapshot(fetchedAt: Date())
        } catch ClaudeUsageClientError.http(401) where allowRefresh {
            _ = try await ClaudeCredentials.refreshPersistedToken()
            return try await fetchOnce(allowRefresh: false)
        }
    }
}
