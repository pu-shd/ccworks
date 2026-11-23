import AppKit
import Darwin
import Foundation
import Sparkle
import SwiftUI

@MainActor
final class SetupModel: ObservableObject {
    @Published var state = SetupState()
    @Published private(set) var canCheckForUpdates = false

    private var child: Process?
    private var commands: FileHandle?
    private let updater: SPUStandardUpdaterController?
    private var observation: NSKeyValueObservation?

    var updatesEnabled: Bool { updater != nil }

    init() {
        if Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") != nil {
            let controller = SPUStandardUpdaterController(
                startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil
            )
            updater = controller
            observation = controller.updater.observe(
                \.canCheckForUpdates, options: [.initial, .new]
            ) { [weak self] _, change in
                let available = change.newValue ?? false
                DispatchQueue.main.async { self?.canCheckForUpdates = available }
            }
        } else {
            updater = nil
        }
    }

    func checkForUpdates() {
        guard !state.phase.busy else { return }
        updater?.checkForUpdates(nil)
    }

    func signIn() {
        state = SetupState(phase: .opening, message: "Opening your approved browser…")
        launch("sign-in")
    }

    func respond(done: Bool) {
        let phase = state.phase
        guard phase == .waiting || (phase == .opening && !done) else { return }
        state = SetupState(
            phase: done ? .verifying : .cancelling,
            message: done ? "Verifying sign-in…" : "Cancelling sign-in…"
        )
        if let commands {
            try? commands.write(contentsOf: Data((done ? "done\n" : "cancel\n").utf8))
            try? commands.close()
        }
        commands = nil
    }

    func removeData() {
        state = SetupState(phase: .removing, message: "Removing ccworks data…")
        launch("remove-data")
    }

    private func launch(_ action: String) {
        let process = Process()
        process.executableURL = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/ccworks")
        process.arguments = ["desktop", action]
        let output = Pipe()
        process.standardOutput = output
        let input = Pipe()
        if action == "sign-in" {
            process.standardInput = input
        } else {
            process.standardInput = FileHandle.nullDevice
        }
        process.standardError = action == "remove-data" ? output : FileHandle.nullDevice

        do {
            try process.run()
        } catch {
            state = SetupState(
                phase: .failed,
                message: "ccworks Desktop could not start its helper. Install an updated copy and try again."
            )
            return
        }
        child = process
        commands = action == "sign-in" ? input.fileHandleForWriting : nil
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let reader = output.fileHandleForReading
            let completion: HelperEvent?
            let removalError: String
            if action == "sign-in" {
                completion = HelperEvent.read(from: reader) { event in
                    DispatchQueue.main.async { self?.state.receive(event) }
                }
                removalError = ""
            } else {
                completion = nil
                removalError = (String(data: reader.readDataToEndOfFile(), encoding: .utf8) ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            }
            process.waitUntilExit()
            let exit = process.terminationStatus
            DispatchQueue.main.async {
                self?.finished(action, exit: exit, event: completion, removalError: removalError)
            }
        }
    }

    private func finished(_ action: String, exit: Int32, event: HelperEvent?, removalError: String) {
        try? commands?.close()
        commands = nil
        child = nil
        if action == "remove-data" {
            if exit == 0, let domain = Bundle.main.bundleIdentifier {
                UserDefaults.standard.removePersistentDomain(forName: domain)
            }
            let busy = "ccworks is busy. Wait for the current operation to finish and try again."
            state = exit == 0
                ? SetupState(phase: .idle, message: "ccworks data removed.")
                : SetupState(phase: .failed, message: removalError == busy
                    ? busy : "ccworks data could not be removed. Contact IT and mention ccworks.")
        } else if let event, event.status != .ready || exit == 0 {
            state.receive(event)
        } else {
            state = SetupState(phase: .failed, message: "Sign-in ended unexpectedly. Start sign-in again.")
        }
    }
}

struct SetupView: View {
    @ObservedObject var model: SetupModel
    @State private var confirmRemoval = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Label("SAP Concur sign-in", systemImage: "person.crop.circle.badge.checkmark")
                .font(.title2.weight(.semibold))
            HStack(alignment: .top, spacing: 10) {
                if model.state.phase.busy && model.state.phase != .waiting {
                    ProgressView().controlSize(.small)
                } else if model.state.phase == .ready {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                } else if model.state.phase == .failed {
                    Image(systemName: "exclamationmark.circle").foregroundStyle(.secondary)
                }
                Text(model.state.message)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                Button("Remove ccworks data") { confirmRemoval = true }
                    .disabled(model.state.phase.busy)
                Spacer()
                if model.state.phase.busy {
                    Button("Cancel") { model.respond(done: false) }
                        .disabled(model.state.phase != .waiting && model.state.phase != .opening)
                        .keyboardShortcut(.cancelAction)
                    Button("Done") { model.respond(done: true) }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.state.phase != .waiting)
                        .keyboardShortcut(.defaultAction)
                } else {
                    Button("Sign in to Concur") { model.signIn() }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(24)
        .frame(width: 480)
        .alert("Remove ccworks data?", isPresented: $confirmRemoval) {
            Button("Cancel", role: .cancel) {}
            Button("Remove", role: .destructive) { model.removeData() }
        } message: {
            Text("This removes the saved sign-in and temporary ccworks data on this Mac.")
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // The helper can close its input just as the user clicks Cancel.
        signal(SIGPIPE, SIG_IGN)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct CCWorksApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var model = SetupModel()

    var body: some Scene {
        Window("ccworks Desktop", id: "setup") {
            SetupView(model: model)
        }
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(after: .appInfo) {
                if model.updatesEnabled {
                    Button("Check for Updates…") { model.checkForUpdates() }
                        .disabled(!model.canCheckForUpdates || model.state.phase.busy)
                }
            }
        }
    }
}
