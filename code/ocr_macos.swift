// Local PDF page OCR. Usage: ocr-macos input.pdf [zero-based-page-indices]
import Foundation
import AppKit
import PDFKit
import Vision

struct PageResult: Codable {
    let page: Int
    let text: String
    let mean_confidence: Float?
    let minimum_confidence: Float?
}

do {
    guard CommandLine.arguments.count >= 2,
          let document = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else {
        throw NSError(domain: "school-safety-ocr", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "Cannot open PDF input"])
    }
    let selected: [Int] = CommandLine.arguments.count > 2
        ? CommandLine.arguments[2].split(separator: ",").compactMap { Int($0) }
        : Array(0..<document.pageCount)
    var results: [PageResult] = []
    for index in selected {
        try autoreleasepool {
            guard let page = document.page(at: index) else {
                throw NSError(domain: "school-safety-ocr", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "PDF page missing"])
            }
            let bounds = page.bounds(for: .mediaBox)
            let size = NSSize(width: bounds.width * 200.0 / 72.0,
                              height: bounds.height * 200.0 / 72.0)
            let image = page.thumbnail(of: size, for: .mediaBox)
            guard let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
                throw NSError(domain: "school-safety-ocr", code: 3,
                              userInfo: [NSLocalizedDescriptionKey: "PDF rendering failed"])
            }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = ["en-US"]
            request.usesLanguageCorrection = true
            try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
            let observations = request.results as? [VNRecognizedTextObservation] ?? []
            let candidates = observations.compactMap { $0.topCandidates(1).first }
            let scores = candidates.map { $0.confidence }
            results.append(PageResult(page: index + 1,
                                      text: candidates.map { $0.string }.joined(separator: "\n"),
                                      mean_confidence: scores.isEmpty ? nil : scores.reduce(0,+)/Float(scores.count),
                                      minimum_confidence: scores.min()))
        }
    }
    FileHandle.standardOutput.write(try JSONEncoder().encode(results))
} catch {
    FileHandle.standardError.write(Data("OCR failed: \(error.localizedDescription)\n".utf8))
    exit(1)
}
