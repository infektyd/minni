import Foundation

protocol ProviderAdapter: Sendable {
    var kind: ProviderKind { get }
    func fetch() async -> ProviderSnapshot
}
