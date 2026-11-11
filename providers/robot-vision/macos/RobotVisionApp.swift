import AppKit
import Foundation
import Network
import Darwin

// The native application remains alive while Python runs as its owned child.
// This process's network probe is never evidence of the child's RTSP health.
private let expectedBundleID = "org.biomerte.robot-vision"

private struct CameraProbe: Decodable {
    let enabled: Bool
    let host: String
    let port: UInt16
}

private struct LauncherConfig: Decodable {
    let schema_version: Int
    let bundle_identifier: String
    let python: String
    let service_script: String
    let service_config: String
    let state_dir: String
    let working_directory: String
    let environment: [String: String]
    let camera_probe: CameraProbe
    let show_status_item: Bool
}

private enum LauncherError: Error, CustomStringConvertible {
    case invalid(String)
    case alreadyRunning
    var description: String {
        switch self {
        case .invalid(let reason): return reason
        case .alreadyRunning: return "A wrapper already owns this vision state."
        }
    }
}

private func readConfiguration(_ path: String) throws -> LauncherConfig {
    guard geteuid() != 0 else { throw LauncherError.invalid("Robot Vision must run in the user session, never as root.") }
    guard path.hasPrefix("/") else { throw LauncherError.invalid("Launcher configuration must be an absolute path.") }
    let fd = Darwin.open(path, O_RDONLY | O_NOFOLLOW)
    guard fd >= 0 else { throw LauncherError.invalid("Cannot open owner-only launcher configuration.") }
    defer { Darwin.close(fd) }
    var info = stat()
    guard fstat(fd, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFREG,
          info.st_uid == geteuid(),
          (info.st_mode & 0o077) == 0 else {
        throw LauncherError.invalid("Launcher configuration must be a regular owner-only file owned by this user.")
    }
    let file = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
    guard let data = try file.readToEnd() else { throw LauncherError.invalid("Empty launcher configuration.") }
    let config = try JSONDecoder().decode(LauncherConfig.self, from: data)
    guard config.schema_version == 1, config.bundle_identifier == expectedBundleID else {
        throw LauncherError.invalid("Unsupported launcher configuration or mismatched application identity.")
    }
    for path in [config.python, config.service_script, config.service_config,
                 config.state_dir, config.working_directory] {
        guard path.hasPrefix("/"), !path.contains("\0") else {
            throw LauncherError.invalid("Child and state paths must be absolute.")
        }
    }
    guard FileManager.default.isExecutableFile(atPath: config.python),
          FileManager.default.fileExists(atPath: config.service_script) else {
        throw LauncherError.invalid("Configured Python or vision service is unavailable.")
    }
    guard config.camera_probe.port > 0, !config.camera_probe.host.isEmpty else {
        throw LauncherError.invalid("Camera probe host and port are required.")
    }
    for (key, value) in config.environment {
        guard !key.isEmpty, !key.contains("="), !key.contains("\0"), !value.contains("\0") else {
            throw LauncherError.invalid("Invalid child environment.")
        }
    }
    return config
}

private func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
        FileHandle.standardError.write(data)
        FileHandle.standardError.write(Data("\n".utf8))
    }
}

private final class VisionApplication: NSObject, NSApplicationDelegate {
    private let config: LauncherConfig
    private var child: Process?
    private var lockFD: Int32 = -1
    private var signalSources: [DispatchSourceSignal] = []
    private var statusItem: NSStatusItem?
    private var processItem: NSMenuItem?
    private var networkItem: NSMenuItem?
    private var probe: NWConnection?
    private var probeFinished = false
    private var stopping = false
    private var awaitingTerminationReply = false
    private var childState = "not_started"
    private var childExitStatus: Int32?
    private var stopReason: String?
    private var network: [String: Any] = [
        "state": "not_started", "scope": "native_parent_only",
        "child_capture_verified": false
    ]

    init(config: LauncherConfig) { self.config = config }

    private var statusURL: URL {
        URL(fileURLWithPath: config.state_dir).appendingPathComponent("macos-app/status.json")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            guard Bundle.main.bundleIdentifier == expectedBundleID else {
                throw LauncherError.invalid("Run the native executable from its prepared application bundle.")
            }
            try acquireOwnership()
            if config.show_status_item { createMenu() }
            else { NSApp.setActivationPolicy(.prohibited) }
            try startChild()
            installSignalHandlers()
            startProbe()
            writeStatus()
        } catch LauncherError.alreadyRunning {
            emit(["event": "already_running", "state_dir": config.state_dir])
            Darwin.exit(0)
        } catch {
            emit(["event": "launcher_failed", "error": String(describing: error)])
            Darwin.exit(78)
        }
    }

    private func acquireOwnership() throws {
        var directoryInfo = stat()
        guard lstat(config.state_dir, &directoryInfo) == 0,
              (directoryInfo.st_mode & S_IFMT) == S_IFDIR,
              directoryInfo.st_uid == geteuid(),
              (directoryInfo.st_mode & 0o022) == 0 else {
            throw LauncherError.invalid("State must be an owned directory without group or other write access.")
        }
        let path = URL(fileURLWithPath: config.state_dir).appendingPathComponent("launcher.lock").path
        let fd = Darwin.open(path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw LauncherError.invalid("Cannot open launcher ownership lock.") }
        var fileInfo = stat()
        guard fstat(fd, &fileInfo) == 0, fileInfo.st_uid == geteuid(),
              (fileInfo.st_mode & S_IFMT) == S_IFREG else {
            Darwin.close(fd)
            throw LauncherError.invalid("Unsafe launcher ownership lock.")
        }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            let code = errno
            Darwin.close(fd)
            if code == EWOULDBLOCK { throw LauncherError.alreadyRunning }
            throw LauncherError.invalid("Cannot lock vision state.")
        }
        fchmod(fd, 0o600)
        lockFD = fd
        let directory = statusURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
    }

    private func createMenu() {
        NSApp.setActivationPolicy(.accessory)
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "Robot Vision"
        let menu = NSMenu()
        let process = NSMenuItem(title: "Vision process: starting", action: nil, keyEquivalent: "")
        let network = NSMenuItem(title: "Camera network: checking app access", action: nil, keyEquivalent: "")
        menu.addItem(process)
        menu.addItem(network)
        menu.addItem(NSMenuItem.separator())
        let settings = NSMenuItem(title: "Open System Settings…", action: #selector(openSettings), keyEquivalent: "")
        settings.target = self
        menu.addItem(settings)
        menu.addItem(NSMenuItem(title: "Privacy & Security → Local Network", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        let quit = NSMenuItem(title: "Quit Robot Vision", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        item.menu = menu
        statusItem = item
        processItem = process
        networkItem = network
    }

    @objc private func openSettings() {
        // Use the installed application, rather than an undocumented pane URL.
        NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/System Settings.app"))
    }

    @objc private func quit() {
        requestStop(reason: "user_quit")
    }

    private func startChild() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: config.python)
        process.arguments = [config.service_script, "--state", config.state_dir,
                             "--config", config.service_config, "--parent-pid", String(getpid())]
        process.currentDirectoryURL = URL(fileURLWithPath: config.working_directory)
        process.environment = config.environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            // AppKit may run a modal termination loop. Deliver in its run-loop
            // modes rather than enqueue behind the main-dispatch termination call.
            RunLoop.main.perform(inModes: [.common, .modalPanel]) { self?.childFinished(status) }
            CFRunLoopWakeUp(CFRunLoopGetMain())
        }
        child = process
        childState = "starting"
        try process.run()
        childState = "running"
        emit(["event": "child_started", "launcher_pid": getpid(),
              "child_pid": process.processIdentifier, "scope": "process_lifecycle_only"])
    }

    private func installSignalHandlers() {
        // Install after spawning so Python doesn't inherit ignored SIGTERM/SIGINT.
        for number in [SIGTERM, SIGINT] {
            Darwin.signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [weak self] in
                self?.requestStop(reason: "signal_\(number)")
            }
            source.resume()
            signalSources.append(source)
        }
    }

    private func requestStop(reason: String) {
        guard !stopping else { return }
        stopping = true
        stopReason = reason
        probe?.cancel()
        if let child = child, child.isRunning {
            childState = "stopping"
            writeStatus()
            // No processing deadline and no escalation to SIGKILL.
            child.terminate()
        } else {
            NSApp.terminate(nil)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if let child = child, child.isRunning {
            awaitingTerminationReply = true
            requestStop(reason: stopReason ?? "application_quit")
            return .terminateLater
        }
        stopping = true
        childState = "stopped"
        writeStatus()
        return .terminateNow
    }

    private func childFinished(_ status: Int32) {
        childExitStatus = status
        childState = stopping ? "stopped" : "unexpected_exit"
        writeStatus()
        if stopping {
            // Foundation has observed the child's exit. Finish ownership before
            // leaving, without entering another AppKit termination cycle.
            releaseOwnership()
            Darwin.exit(0)
        } else {
            // Even an unsolicited exit(0) is failure of this persistent service.
            emit(["event": "unexpected_child_exit", "child_exit_status": status])
            Darwin.exit(status == 0 ? 1 : status)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        probe?.cancel()
        // This callback is reached only after the owned child is no longer running.
        releaseOwnership()
    }

    private func releaseOwnership() {
        if lockFD >= 0 {
            flock(lockFD, LOCK_UN)
            Darwin.close(lockFD)
            lockFD = -1
        }
    }

    private func startProbe() {
        guard config.camera_probe.enabled else {
            network["state"] = "disabled"
            writeStatus()
            return
        }
        let port = NWEndpoint.Port(rawValue: config.camera_probe.port)!
        let connection = NWConnection(host: NWEndpoint.Host(config.camera_probe.host), port: port, using: .tcp)
        probe = connection
        network["host"] = config.camera_probe.host
        network["port"] = config.camera_probe.port
        network["state"] = "preparing"
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self = self, let connection = connection, !self.probeFinished else { return }
            self.network["assessed_at_mac_utc"] = ISO8601DateFormatter().string(from: Date())
            switch state {
            case .ready:
                self.network["state"] = "ready"
                self.network["reason"] = "native_parent_tcp_connection_succeeded"
                self.probeFinished = true
                connection.cancel()
            case .waiting(let error):
                self.network["state"] = "waiting"
                self.network["error"] = String(describing: error)
                self.network["reason"] = connection.currentPath?.unsatisfiedReason == .localNetworkDenied
                    ? "local_network_denied" : "network_unavailable_or_other_policy"
            case .failed(let error):
                self.network["state"] = "failed"
                self.network["error"] = String(describing: error)
                self.network["reason"] = "connection_failed"
                self.probeFinished = true
            case .cancelled:
                self.network["state"] = "cancelled"
            case .preparing:
                self.network["state"] = "preparing"
            case .setup:
                self.network["state"] = "setup"
            @unknown default:
                self.network["state"] = "unknown"
            }
            self.writeStatus()
        }
        connection.start(queue: .main)
    }

    private func writeStatus() {
        processItem?.title = "Vision process: \(childState)"
        if network["reason"] as? String == "local_network_denied" {
            networkItem?.title = "Camera network: app permission denied"
        } else {
            networkItem?.title = "Camera network (app probe): \(network["state"] ?? "unknown")"
        }
        var value: [String: Any] = [
            "schema_version": 1, "bundle_identifier": expectedBundleID,
            "launcher_pid": getpid(), "child_state": childState,
            "assessed_at_mac_utc": ISO8601DateFormatter().string(from: Date()),
            "parent_network_probe": network,
            "child_capture_health": "query_robot.vision.status",
            "child_pid": child?.processIdentifier ?? 0
        ]
        if let status = childExitStatus { value["child_exit_status"] = status }
        if let reason = stopReason { value["stop_reason"] = reason }
        do {
            let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
            try data.write(to: statusURL, options: [.atomic])
            chmod(statusURL.path, 0o600)
        } catch {
            emit(["event": "status_write_failed", "error": String(describing: error)])
        }
    }
}

private func main() -> Never {
    do {
        let arguments = Array(CommandLine.arguments.dropFirst())
        var path = Bundle.main.object(forInfoDictionaryKey: "RobotVisionLauncherConfig") as? String
        var validateOnly = false
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--launcher-config":
                index += 1
                guard index < arguments.count else { throw LauncherError.invalid("Missing --launcher-config path.") }
                path = arguments[index]
            case "--validate-config": validateOnly = true
            default: throw LauncherError.invalid("Unknown application argument.")
            }
            index += 1
        }
        guard let path = path else { throw LauncherError.invalid("No launcher configuration is configured.") }
        let config = try readConfiguration(path)
        if validateOnly {
            print("{\"valid\":true,\"bundle_identifier\":\"\(expectedBundleID)\"}")
            Darwin.exit(0)
        }
        let app = NSApplication.shared
        let delegate = VisionApplication(config: config)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
        Darwin.exit(0)
    } catch {
        emit(["event": "invalid_launcher_configuration", "error": String(describing: error)])
        Darwin.exit(78)
    }
}
main()
