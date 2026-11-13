"""Compile the small public-release installer and run offline validation only."""
from pathlib import Path
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parent.parent
CSC = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")
OUTPUT = ROOT / "dist/Leanwatts-Testbench-Gen2-Install-Latest.exe"
WORK = ROOT / "artifacts/web-installer-build"
SOURCE = ROOT / "tools/LeanwattsWebSetup.cs"
ASSETS = ROOT / "leanwatts_can/assets"

TESTS = r'''
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;

internal static class WebSetupTests
{
    private static int checks;
    private static void Check(bool result) { checks++; if (!result) throw new Exception("Offline assertion " + checks + " failed."); }
    private static void Reject(Action action)
    { bool rejected = false; try { action(); } catch (InvalidDataException) { rejected = true; } catch (InvalidOperationException) { rejected = true; } Check(rejected); }
    private static string Hash(byte[] bytes)
    { using (SHA256 sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant(); }
    private static string Manifest(string packages)
    { return "{\"schema\":1,\"application\":\"leanwatts-testbench-gen2\",\"version\":\"9.8\",\"packages\":" + packages + "}"; }
    private static Dictionary<string, object> Asset(string name, long id, long size)
    { return new Dictionary<string, object> { {"name", name}, {"id", id}, {"size", size}, {"state", "uploaded"}, {"url", "https://untrusted.invalid/ignored"} }; }

    private static void Main(string[] args)
    {
        byte[] binary = Encoding.ASCII.GetBytes("MZ offline full-installer fixture");
        string hash = Hash(binary);
        string installer = "{\"filename\":\"full.exe\",\"asset_name\":\"full.exe\",\"sha256\":\"" + hash + "\",\"size\":" + binary.Length + "}";
        string full = "{\"package_type\":\"full\",\"installer\":" + installer + "}";
        string delta = "{\"package_type\":\"delta\",\"base_version\":\"9.7\",\"installer\":" + installer.Replace("full.exe", "delta.exe") + "}";
        var assets = new List<Dictionary<string, object>> { Asset("full.exe", 11, binary.Length), Asset("delta.exe", 12, binary.Length) };
        var release = WebSetup.SelectFull(WebSetup.Json(Manifest("[" + delta + "," + full + "]")), assets);
        Check(release.Filename == "full.exe");
        Check(release.Url == WebSetup.ApiRoot + "/releases/assets/11");
        Check(release.Size == binary.Length && release.Version == "9.8" && release.Sha256 == hash);
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + delta + "]")), assets));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "," + full + "]")), assets));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]").Replace("leanwatts-testbench-gen2", "wrong-product")), assets));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]").Replace("9.8", "latest")), assets));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]").Replace("full.exe", "../bad.exe")), assets));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]").Replace("full.exe", "CON.exe")), assets));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]").Replace(hash, "invalid")), assets));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]")), new List<Dictionary<string, object>>()));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]")), new List<Dictionary<string, object>> { Asset("full.exe", 11, binary.Length + 1) }));
        Reject(() => WebSetup.SelectFull(WebSetup.Json(Manifest("[" + full + "]").Replace("\"size\":" + binary.Length, "\"size\":536870913")), assets));
        Check(WebSetup.HttpsUri("https://github.com/releases/x").Scheme == "https");
        Reject(() => WebSetup.HttpsUri("http://github.com/x"));
        Reject(() => WebSetup.HttpsUri("https://user:password@github.com/x"));
        Reject(() => WebSetup.HttpsUri("file:///C:/x.exe"));
        Reject(() => WebSetup.HttpsUri("https://github.com:8443/x"));
        string fixture = Path.Combine(args[0], "offline-installer-fixture.bin");
        File.WriteAllBytes(fixture, binary);
        WebSetup.Verify(fixture, release); Check(true);
        binary[binary.Length - 1] ^= 1; File.WriteAllBytes(fixture, binary);
        Reject(() => WebSetup.Verify(fixture, release));
        File.WriteAllBytes(fixture, Encoding.ASCII.GetBytes("not-an-exe"));
        Reject(() => WebSetup.Verify(fixture, release));
        byte[] notExe = Encoding.ASCII.GetBytes("XX same-size fixture");
        var invalid = new WebSetup.Release { Size = notExe.Length, Sha256 = Hash(notExe) };
        File.WriteAllBytes(fixture, notExe);
        Reject(() => WebSetup.Verify(fixture, invalid));
        Console.WriteLine("{\"checks_passed\":" + checks + ",\"network_access\":false,\"installer_executed\":false,\"hardware_access\":false}");
    }
}
'''

README = """Leanwatts Testbench Gen2 - small web installer

Copy Leanwatts-Testbench-Gen2-Install-Latest.exe to a new Windows laptop.
Open it and choose Download and install latest. It downloads the newest stable
FULL installer from the fixed public GitHub release channel, verifies its size
and SHA-256, then opens Setup. No GitHub login or token is required.

Channel: https://github.com/pradeep-lw/leanwatts-testbench-gen2
The stable release must contain update-manifest.json and the full installer
named in that manifest. A delta-only release cannot install a new laptop.

No release has been published at handover. Until a public release is available,
this web installer displays that fact and cannot download an installation.
Use the complete Leanwatts-Testbench-Gen2-Setup-1.1.exe supplied locally now.

The download is limited to 512 MB and HTTPS redirects only. Damaged, oversized,
wrong-product or unverified downloads are not launched. Downloads are staged
outside the installed application. Existing data is handled by the downloaded
full Setup. Windows protections and device drivers are not changed.

This locally built bootstrapper is unsigned. It requires Windows with .NET
Framework 4.5 or newer and Internet access. It makes no hardware calls.
Offline validation covered metadata selection and hash rejection; no live
GitHub download or installer execution was performed before publication.
"""


def compile(source_paths, output, *, test=False):
    command = [str(CSC), "/nologo", "/target:" + ("exe" if test else "winexe"), "/platform:anycpu", "/optimize+",
               "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.Web.Extensions.dll", "/out:" + str(output)]
    if test:
        command.append("/main:WebSetupTests")
    else:
        command += ["/win32icon:" + str(ASSETS / "leanwatts.ico"),
                    "/resource:" + str(ASSETS / "leanwatts-logo-light.png") + ",branding.png"]
    command += [str(source) for source in source_paths]
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    if OUTPUT.exists():
        raise SystemExit("Preserving the existing web installer.")
    WORK.mkdir(parents=True, exist_ok=True)
    tests = WORK / "WebSetupTests.cs"
    tests.write_text(TESTS, encoding="utf-8")
    test_exe = WORK / "WebSetupTests.exe"
    compile([SOURCE, tests], test_exe, test=True)
    checked = subprocess.run([str(test_exe), str(WORK)], cwd=WORK, capture_output=True, text=True, check=True)
    validation = json.loads(checked.stdout)
    compile([SOURCE], OUTPUT)
    with OUTPUT.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    OUTPUT.with_suffix(".exe.sha256").write_text(checksum + "  " + OUTPUT.name + "\n", encoding="ascii")
    (ROOT / "dist/README-Web-Installer.txt").write_text(README, encoding="utf-8")
    report = dict(artifact=str(OUTPUT), bytes=OUTPUT.stat().st_size, sha256=checksum,
                  repository="pradeep-lw/leanwatts-testbench-gen2", full_installer_only=True,
                  published_release_available=False, code_signed=False, **validation)
    (WORK / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
