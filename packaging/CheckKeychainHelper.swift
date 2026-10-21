// Synthetic regression client. Contains no real email or credentials.
import Foundation
import Security

let mode = CommandLine.arguments[1]
let helper = CommandLine.arguments[2]
let name = CommandLine.arguments[3]
func call(_ operation: String, _ value: String? = nil) -> [String: Any] {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: helper)
    let input = Pipe(), output = Pipe()
    process.standardInput = input
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    try! process.run()
    var request = ["operation": operation, "name": name]
    if let value = value { request["value"] = value }
    input.fileHandleForWriting.write(try! JSONSerialization.data(withJSONObject: request))
    try! input.fileHandleForWriting.close()
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
}
if mode == "seed" {
    guard call("set", "synthetic-helper-test-value")["ok"] as? Bool == true else { exit(1) }
    print("PASS: first signed build saved synthetic helper item")
} else if mode == "verify" {
    guard call("get")["value"] as? String == "synthetic-helper-test-value" else { exit(2) }
    print("PASS: different signed build retrieved synthetic item through unchanged helper")
    guard call("delete")["ok"] as? Bool == true else { exit(3) }
    print("PASS: synthetic test item removed")
} else if mode == "denied" {
    guard call("get")["error"] as? String == "unauthorized_caller" else { exit(4) }
    print("PASS: differently signed app denied before Keychain access")
} else { exit(5) }
