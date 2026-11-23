import AppKit

let directory = URL(fileURLWithPath: CommandLine.arguments[1])
let representations: [(String, Int)] = [
    ("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64),
    ("128x128", 128), ("128x128@2x", 256), ("256x256", 256),
    ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024),
]
for (name, size) in representations {
    let bitmap = CGContext(
        data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    )!
    bitmap.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    let background = CGPath(
        roundedRect: CGRect(x: 48, y: 48, width: 928, height: 928),
        cornerWidth: 220, cornerHeight: 220, transform: nil
    )
    bitmap.addPath(background)
    bitmap.setFillColor(red: 0.086, green: 0.29, blue: 0.34, alpha: 1)
    bitmap.fillPath()

    bitmap.setLineJoin(.round)
    bitmap.setLineCap(.round)
    bitmap.setStrokeColor(red: 0.96, green: 0.99, blue: 0.97, alpha: 1)
    bitmap.setLineWidth(80)
    bitmap.move(to: CGPoint(x: 288, y: 507))
    bitmap.addLine(to: CGPoint(x: 443, y: 360))
    bitmap.addLine(to: CGPoint(x: 734, y: 652))
    bitmap.strokePath()

    let representation = NSBitmapImageRep(cgImage: bitmap.makeImage()!)
    try representation.representation(using: .png, properties: [:])!
        .write(to: directory.appendingPathComponent("icon_\(name).png"))
}
