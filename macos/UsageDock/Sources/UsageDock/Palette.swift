import SwiftUI

enum Palette {
    static let persimmon = Color(red: 0.824, green: 0.376, blue: 0.227)
    static let verdigris = Color(red: 0.184, green: 0.490, blue: 0.408)
    static let mustard = Color(red: 0.788, green: 0.663, blue: 0.380)
    static let blue = Color(red: 0.239, green: 0.435, blue: 0.584)
    static let ink = Color(red: 0.094, green: 0.086, blue: 0.075)
    static let bone = Color(red: 0.957, green: 0.945, blue: 0.918)
    static let secondary = Color.white.opacity(0.55)
    static let track = Color.white.opacity(0.12)

    static func accent(for kind: ProviderKind) -> Color {
        switch kind {
        case .claude: persimmon
        case .chatgpt: verdigris
        case .perplexity: mustard
        case .cursor: blue
        }
    }

    static func bar(for percent: Double, primary: Color) -> Color {
        if percent >= 80 { return persimmon }
        if percent >= 50 { return mustard }
        return primary
    }
}
