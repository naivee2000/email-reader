import Foundation
import Security

// This executable is installed once, outside the rebuilt app bundle. Its
// cdhash must remain unchanged: self-signed macOS clients receive cdhash
// partition ACLs even when their designated requirement is stable.
let appRequirement = "identifier \"com.privateemailreader.app\" and certificate root = H\"630f3bda14d0a6d97d87095eb7aab18ff26d4396\""
let service = "PrivateEmailReader.Helper.v1"

func reply(_ value: [String: Any], exitCode: Int32 = 0) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: value) {
        FileHandle.standardOutput.write(data)
    }
    exit(exitCode)
}

func authorizeCaller() {
    var requirement: SecRequirement?
    var caller: SecCode?
    guard SecRequirementCreateWithString(appRequirement as CFString, [], &requirement) == errSecSuccess,
          SecCodeCopyGuestWithAttributes(nil, [kSecGuestAttributePid: getppid()] as CFDictionary, [], &caller) == errSecSuccess,
          let caller = caller,
          SecCodeCheckValidity(caller, [], requirement) == errSecSuccess else {
        reply(["error": "unauthorized_caller"], exitCode: 1)
    }
}

func query(_ name: String, _ serviceName: String = service) -> [String: Any] {
    return [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceName, kSecAttrAccount as String: name]
}

func read(_ name: String, _ serviceName: String = service) -> String? {
    var request = query(name, serviceName)
    request[kSecReturnData as String] = true
    request[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(request as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = result as? Data,
          let value = String(data: data, encoding: .utf8) else {
        reply(["error": "keychain_access", "status": status], exitCode: 2)
    }
    return value
}

func write(_ name: String, _ value: String) {
    let attributes = [kSecValueData as String: Data(value.utf8)]
    var status = SecItemUpdate(query(name) as CFDictionary, attributes as CFDictionary)
    if status == errSecItemNotFound {
        var request = query(name)
        request[kSecValueData as String] = Data(value.utf8)
        status = SecItemAdd(request as CFDictionary, nil)
    }
    guard status == errSecSuccess else {
        reply(["error": "keychain_write", "status": status], exitCode: 2)
    }
}

authorizeCaller()
let input = FileHandle.standardInput.readDataToEndOfFile()
guard input.count <= 4_000_000,
      let request = try? JSONSerialization.jsonObject(with: input) as? [String: String],
      let operation = request["operation"], let name = request["name"],
      !name.isEmpty, name.count <= 512 else {
    reply(["error": "invalid_request"], exitCode: 1)
}

switch operation {
case "get":
    if let value = read(name) { reply(["value": value]) }
    // A tombstone prevents a deleted helper value from resurrecting a legacy copy.
    if read("deleted:" + name) != nil { reply(["value": NSNull()]) }
    for legacy in ["PrivateEmailReader.Stable.v1", "PrivateEmailReader"] {
        if let value = read(name, legacy) {
            write(name, value)
            guard read(name) == value else { reply(["error": "migration_verification"], exitCode: 2) }
            reply(["value": value])
        }
    }
    reply(["value": NSNull()])
case "set":
    guard let value = request["value"] else { reply(["error": "invalid_request"], exitCode: 1) }
    write(name, value)
    reply(["ok": true])
case "delete":
    write("deleted:" + name, "1")
    let status = SecItemDelete(query(name) as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
        reply(["error": "keychain_delete", "status": status], exitCode: 2)
    }
    reply(["ok": true])
default:
    reply(["error": "invalid_operation"], exitCode: 1)
}
