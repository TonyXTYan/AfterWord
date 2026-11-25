// afterword-avspeech: Apple's speech synthesizer (AVSpeechSynthesizer) for
// AfterWord. The daemon compiles this file on first use (swiftc) and runs it
// once per line; see afterword/daemon/apple.py.
//
//   afterword-avspeech --list
//       Installed voices as one JSON array: id, name, lang, quality
//       ("default" | "enhanced" | "premium"), novelty, personal.
//
//   afterword-avspeech [--voice <id or name>] [--lang <bcp-47>] < text
//       Speaks the UTF-8 text on stdin into stdout: the 6 bytes "AWPCM1", a
//       little-endian uint32 sample rate, then mono float32 little-endian
//       samples as the synthesizer makes them. No --voice (or an empty one):
//       the best installed voice for --lang. Exit 2: no such voice; 3: no
//       audio was made; 4: timed out.

import AVFoundation
import Foundation

setvbuf(stdout, nil, _IOFBF, 1 << 16)

func fail(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

func qualityName(_ q: AVSpeechSynthesisVoiceQuality) -> String {
    switch q {
    case .premium: return "premium"
    case .enhanced: return "enhanced"
    default: return "default"
    }
}

func rank(_ v: AVSpeechSynthesisVoice) -> Int {
    switch v.quality {
    case .premium: return 3
    case .enhanced: return 2
    default: return 1
    }
}

func isPersonal(_ v: AVSpeechSynthesisVoice) -> Bool {
    if #available(macOS 14.0, *) { return v.voiceTraits.contains(.isPersonalVoice) }
    return false
}

func isNovelty(_ v: AVSpeechSynthesisVoice) -> Bool {
    if #available(macOS 14.0, *) { return v.voiceTraits.contains(.isNoveltyVoice) }
    return false
}

struct Locale3 {
    var lang = ""
    var script: String? = nil
    var region: String? = nil
}

func parseTag(_ tag: String) -> Locale3 {
    var out = Locale3()
    let parts = tag.replacingOccurrences(of: "_", with: "-").split(separator: "-").map(String.init)
    if let first = parts.first { out.lang = first.lowercased() }
    for part in parts.dropFirst() {
        if part.count == 4, part.allSatisfy({ $0.isLetter }) { out.script = part.lowercased() }
        else if (part.count == 2 && part.allSatisfy({ $0.isLetter })) || (part.count == 3 && part.allSatisfy({ $0.isNumber })) {
            out.region = part.uppercased()
        }
    }
    return out
}

/// Regions a script implies when a voice's locale has no script subtag.
let scriptRegions: [String: Set<String>] = [
    "hant": ["TW", "HK", "MO"],
    "hans": ["CN", "SG"],
]

/// "zh" matches every zh voice; "zh-TW" only zh-TW; "zh-Hant-TW" matches a
/// voice reporting zh-TW (voices rarely carry a script); "zh-Hant" matches
/// zh-TW, zh-HK and zh-MO.
func langMatches(_ voiceLang: String, _ want: String) -> Bool {
    let v = parseTag(voiceLang), w = parseTag(want)
    if v.lang != w.lang { return false }
    if let ws = w.script {
        if let vs = v.script {
            if vs != ws { return false }
        } else if w.region == nil, let regions = scriptRegions[ws] {
            guard let vr = v.region, regions.contains(vr) else { return false }
        }
    }
    if let wr = w.region, v.region != wr { return false }
    return true
}

func pickVoice(_ spec: String, _ lang: String) -> AVSpeechSynthesisVoice? {
    let all = AVSpeechSynthesisVoice.speechVoices().filter { !isPersonal($0) }
    if !spec.isEmpty {
        if let v = AVSpeechSynthesisVoice(identifier: spec) { return v }
        // By name, e.g. "Ava" or "Ava (Premium)": the best quality, in --lang if given.
        let bare = spec.replacingOccurrences(of: #"\s*\((Premium|Enhanced|Default)\)$"#, with: "",
                                             options: [.regularExpression, .caseInsensitive])
        var named = all.filter { $0.name.caseInsensitiveCompare(bare) == .orderedSame }
        if let q = spec.range(of: #"\((Premium|Enhanced)\)$"#, options: [.regularExpression, .caseInsensitive]) {
            let want = spec[q].dropFirst().dropLast().lowercased()
            let exact = named.filter { qualityName($0.quality) == want }
            if !exact.isEmpty { named = exact }
        }
        if !lang.isEmpty {
            let inLang = named.filter { langMatches($0.language, lang) }
            if !inLang.isEmpty { named = inLang }
        }
        return named.max { rank($0) < rank($1) }
    }
    if lang.isEmpty { return AVSpeechSynthesisVoice(language: nil) }
    let candidates = all.filter { langMatches($0.language, lang) && !isNovelty($0) }
    if candidates.isEmpty { return nil }
    // The system's default region for the language first (zh -> zh-CN), then quality.
    let region = AVSpeechSynthesisVoice(language: lang)?.language
        ?? AVSpeechSynthesisVoice(language: parseTag(lang).lang)?.language
    return candidates.max { a, b in
        let ra = (a.language == region ? 10 : 0) + rank(a)
        let rb = (b.language == region ? 10 : 0) + rank(b)
        return ra < rb
    }
}

var args = Array(CommandLine.arguments.dropFirst())
var voiceSpec = ""
var lang = ""
var list = false
while !args.isEmpty {
    let a = args.removeFirst()
    switch a {
    case "--list": list = true
    case "--voice": voiceSpec = args.isEmpty ? "" : args.removeFirst()
    case "--lang": lang = args.isEmpty ? "" : args.removeFirst()
    default: fail(64, "unknown argument \(a)")
    }
}

if list {
    let items: [[String: Any]] = AVSpeechSynthesisVoice.speechVoices().map {
        ["id": $0.identifier, "name": $0.name, "lang": $0.language, "quality": qualityName($0.quality),
         "novelty": isNovelty($0), "personal": isPersonal($0)]
    }
    let data = try! JSONSerialization.data(withJSONObject: items, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    exit(0)
}

let input = FileHandle.standardInput.readDataToEndOfFile()
let text = String(decoding: input, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
if text.isEmpty { fail(3, "no text on stdin") }
guard let voice = pickVoice(voiceSpec, lang) else {
    fail(2, voiceSpec.isEmpty ? "no installed voice for language \(lang)" : "no voice named \(voiceSpec)")
}

final class Writer: NSObject, AVSpeechSynthesizerDelegate {
    let lock = NSLock()
    var headerWritten = false
    var frames = 0
    var finished = false

    func write(_ buffer: AVAudioBuffer) {
        lock.lock()
        defer { lock.unlock() }
        guard let pcm = buffer as? AVAudioPCMBuffer else { return }
        let n = Int(pcm.frameLength)
        if n == 0 { finish(); return }
        if !headerWritten {
            var rate = UInt32(pcm.format.sampleRate).littleEndian
            fwrite("AWPCM1", 1, 6, stdout)
            fwrite(&rate, 4, 1, stdout)
            headerWritten = true
        }
        var samples = [Float](repeating: 0, count: n)
        if let f = pcm.floatChannelData {
            for i in 0..<n { samples[i] = f[0][i] }
        } else if let s = pcm.int16ChannelData {
            for i in 0..<n { samples[i] = Float(s[0][i]) / 32768.0 }
        } else if let s = pcm.int32ChannelData {
            for i in 0..<n { samples[i] = Float(s[0][i]) / 2147483648.0 }
        } else {
            return
        }
        frames += n
        samples.withUnsafeBufferPointer { _ = fwrite($0.baseAddress, 4, n, stdout) }
        fflush(stdout)
    }

    func finish() {
        if finished { return }
        finished = true
        fflush(stdout)
        exit(frames > 0 ? 0 : 3)
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        // Some voices end without an empty buffer; let late buffers land first.
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) {
            self.lock.lock()
            self.finish()
        }
    }
}

let writer = Writer()
let synthesizer = AVSpeechSynthesizer()
synthesizer.delegate = writer
let utterance = AVSpeechUtterance(string: text)
utterance.voice = voice
synthesizer.write(utterance) { buffer in writer.write(buffer) }
DispatchQueue.global().asyncAfter(deadline: .now() + 120) { fail(4, "timed out") }
RunLoop.main.run()
