import Foundation

enum SetupPhase: Equatable {
    case idle, opening, waiting, verifying, cancelling, removing, ready, failed

    var busy: Bool {
        switch self {
        case .opening, .waiting, .verifying, .cancelling, .removing: return true
        default: return false
        }
    }
}

struct HelperEvent: Decodable {
    enum Kind: String, Decodable { case waiting, finished }
    enum Status: String, Decodable { case ready, cancelled, failed }

    let event: Kind
    let status: Status?
    let message: String?

    static func decode(_ line: Data) -> HelperEvent? {
        try? JSONDecoder().decode(Self.self, from: line)
    }
}

struct SetupState {
    var phase: SetupPhase = .idle
    var message = "Sign in to connect ccworks to SAP Concur."

    mutating func receive(_ event: HelperEvent) {
        switch event.event {
        case .waiting where phase == .opening:
            phase = .waiting
            message = "Complete sign-in in the browser. When the Concur expense dashboard appears, "
                + "return here and click Done."
        case .finished:
            switch event.status {
            case .ready: phase = .ready
            case .cancelled: phase = .idle
            default: phase = .failed
            }
            message = event.message ?? "ccworks could not complete sign-in."
        default:
            break
        }
    }
}

extension HelperEvent {
    static func read(from input: FileHandle, onProgress: (HelperEvent) -> Void) -> HelperEvent? {
        var pending = Data()
        var terminal: HelperEvent?
        while true {
            let chunk = input.availableData
            if chunk.isEmpty { return terminal }
            pending.append(chunk)
            while let newline = pending.firstIndex(of: 10) {
                if let event = decode(Data(pending[..<newline])) {
                    if event.event == .finished {
                        terminal = event
                    } else {
                        onProgress(event)
                    }
                }
                pending.removeSubrange(...newline)
            }
            if pending.count > 4096 { pending.removeAll() }
        }
    }
}
