import Foundation

@main
struct ProtocolTests {
    static func require(_ result: Bool, _ message: String) {
        if !result {
            print("FAIL: \(message)")
            exit(1)
        }
    }

    static func main() throws {
        let pipe = Pipe()
        let messages = """
        {"event":"waiting"}
        an unrelated message
        {"event":"finished","status":"ready","message":"Ready."}

        """
        try pipe.fileHandleForWriting.write(contentsOf: Data(messages.utf8))
        try pipe.fileHandleForWriting.close()
        var state = SetupState(phase: .opening)
        let terminal = HelperEvent.read(from: pipe.fileHandleForReading) { event in
            state.receive(event)
        }
        require(state.phase == .waiting, "Chrome readiness must enable Done")
        guard let terminal else {
            require(false, "the terminal event must survive pipe closure")
            return
        }
        state.receive(terminal)
        require(state.phase == .ready && state.message == "Ready.", "the UI must show verified sign-in")

        state.phase = .cancelling
        state.receive(HelperEvent(event: .waiting, status: nil, message: nil))
        require(state.phase == .cancelling, "a late Chrome event must not cancel the user's cancellation")
        state.receive(HelperEvent(event: .finished, status: .cancelled, message: "Cancelled."))
        require(state.phase == .idle && state.message == "Cancelled.", "cancellation must return to idle")

        state.receive(HelperEvent(event: .finished, status: .failed, message: "Try again."))
        require(state.phase == .failed && !state.phase.busy, "failure must allow retry")
        print("PASS: SwiftUI helper event decoding, readiness, cancellation, and failure")
    }
}
