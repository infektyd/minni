import SwiftUI

/// Original marks. Not vendor logos.
struct ProviderMark: View {
    var kind: ProviderKind
    var size: CGFloat = 22

    var body: some View {
        Canvas { context, canvasSize in
            let rect = CGRect(origin: .zero, size: canvasSize)
            switch kind {
            case .claude:
                drawAsterisk(context: context, in: rect)
            case .chatgpt:
                drawHexKnot(context: context, in: rect)
            case .perplexity:
                drawFourDiamond(context: context, in: rect)
            case .cursor:
                drawCaret(context: context, in: rect)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private func drawAsterisk(context: GraphicsContext, in rect: CGRect) {
        var path = Path()
        let center = CGPoint(x: rect.midX, y: rect.midY)
        let outer = min(rect.width, rect.height) * 0.42
        for i in 0..<8 {
            let angle = Double(i) * .pi / 4 - .pi / 2
            let point = CGPoint(
                x: center.x + CGFloat(cos(angle)) * outer,
                y: center.y + CGFloat(sin(angle)) * outer
            )
            path.move(to: center)
            path.addLine(to: point)
        }
        context.stroke(path, with: .color(.white), style: StrokeStyle(lineWidth: 2.2, lineCap: .round))
    }

    private func drawHexKnot(context: GraphicsContext, in rect: CGRect) {
        var path = Path()
        let center = CGPoint(x: rect.midX, y: rect.midY)
        let radius = min(rect.width, rect.height) * 0.38
        var points: [CGPoint] = []
        for i in 0..<6 {
            let angle = Double(i) * .pi / 3 - .pi / 2
            points.append(
                CGPoint(
                    x: center.x + CGFloat(cos(angle)) * radius,
                    y: center.y + CGFloat(sin(angle)) * radius
                )
            )
        }
        path.addLines(points)
        path.closeSubpath()
        context.stroke(path, with: .color(.white), style: StrokeStyle(lineWidth: 1.8, lineJoin: .round))
        var inner = Path()
        inner.addEllipse(in: rect.insetBy(dx: rect.width * 0.32, dy: rect.height * 0.32))
        context.stroke(inner, with: .color(.white), style: StrokeStyle(lineWidth: 1.4))
    }

    private func drawFourDiamond(context: GraphicsContext, in rect: CGRect) {
        var path = Path()
        let center = CGPoint(x: rect.midX, y: rect.midY)
        let arm = min(rect.width, rect.height) * 0.40
        path.move(to: CGPoint(x: center.x, y: center.y - arm))
        path.addLine(to: CGPoint(x: center.x + arm * 0.38, y: center.y))
        path.addLine(to: CGPoint(x: center.x, y: center.y + arm))
        path.addLine(to: CGPoint(x: center.x - arm * 0.38, y: center.y))
        path.closeSubpath()
        var cross = Path()
        cross.move(to: CGPoint(x: center.x - arm * 0.55, y: center.y - arm * 0.18))
        cross.addLine(to: CGPoint(x: center.x + arm * 0.55, y: center.y + arm * 0.18))
        cross.move(to: CGPoint(x: center.x + arm * 0.55, y: center.y - arm * 0.18))
        cross.addLine(to: CGPoint(x: center.x - arm * 0.55, y: center.y + arm * 0.18))
        context.stroke(path, with: .color(.white), style: StrokeStyle(lineWidth: 1.6, lineJoin: .round))
        context.stroke(cross, with: .color(.white), style: StrokeStyle(lineWidth: 1.4, lineCap: .round))
    }

    private func drawCaret(context: GraphicsContext, in rect: CGRect) {
        var path = Path()
        let inset = rect.insetBy(dx: rect.width * 0.22, dy: rect.height * 0.20)
        path.move(to: CGPoint(x: inset.minX, y: inset.minY))
        path.addLine(to: CGPoint(x: inset.maxX - inset.width * 0.15, y: inset.midY))
        path.addLine(to: CGPoint(x: inset.minX, y: inset.maxY))
        context.stroke(path, with: .color(.white), style: StrokeStyle(lineWidth: 2.0, lineCap: .round, lineJoin: .round))
    }
}
