"""Synthetic delta packaging/staging checks; never invokes Installer.Install or Main."""
from __future__ import annotations
import json
import argparse
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_gen2_delta as delta


def release(path, version, contents):
    files = dict(contents)
    files["VERSION.json"] = json.dumps({"application": delta.APPLICATION, "version": version}).encode()
    manifest = {name: delta.digest(data) for name, data in files.items()}
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(delta.PREFIX + name, data)
        archive.writestr(delta.PREFIX + "MANIFEST.json", json.dumps(manifest, indent=2) + "\n")


HARNESS = r'''using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
class StagingChecks {
 static Type installer;
 static string root;
 static void Set(string name, object value) { installer.GetField(name, BindingFlags.Static|BindingFlags.NonPublic).SetValue(null, value); }
 static void Extract(string archive, string stage) {
  Directory.CreateDirectory(stage);
  using(var stream=File.OpenRead(archive)) using(var zip=new ZipArchive(stream,ZipArchiveMode.Read))
   installer.GetMethod("ExtractPackage",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{zip,stage,(Action<string>)(s=>{})});
 }
 static void Assert(bool condition,string message) { if(!condition) throw new Exception(message); }
 static void Main(string[] args) {
  root=Path.GetFullPath(args[0]);
  if(!root.EndsWith("delta-synthetic-checks",StringComparison.Ordinal)) throw new Exception("Unexpected fixture root");
  installer=Assembly.LoadFile(Path.GetFullPath(args[1])).GetType("Installer",true);
  string programs=Path.Combine(root,"Programs");
  string target=Path.Combine(programs,"Leanwatts-Testbench-Gen2");
  Set("Programs",programs); Set("Target",target);
  string stage=Path.Combine(programs,"Leanwatts-Testbench-Gen2.install-check");
  Extract(args[2],stage);
  Assert(File.ReadAllText(Path.Combine(stage,"app.py"))=="new application", "Changed file not overlaid");
  Assert(File.Exists(Path.Combine(stage,"unchanged.bin")),"Unchanged file not copied");
  Assert(File.ReadAllText(Path.Combine(stage,"runtime","python.exe"))=="unchanged runtime fixture", "Unchanged runtime not copied");
  Assert(File.Exists(Path.Combine(stage,"added.txt")),"New file missing");
  Assert(!File.Exists(Path.Combine(stage,"removed.txt")),"Removed application file copied");
  Assert(!Directory.Exists(Path.Combine(stage,"calibration_data")),"Station data entered application staging");
  Assert(!File.Exists(Path.Combine(stage,".app-running.lock")),"Lock copied");
  Assert(!File.Exists(Path.Combine(stage,"untracked.txt")),"Untracked file copied");
  Assert(File.ReadAllText(Path.Combine(target,"calibration_data","profiles.json"))=="station data", "Station data modified");
  installer.GetMethod("CopyData",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{Path.Combine(target,"calibration_data"),Path.Combine(stage,"calibration_data"),true});
  Assert(File.ReadAllText(Path.Combine(stage,"calibration_data","profiles.json"))=="station data","Station data not preserved");
  Assert(File.Exists(Path.Combine(stage,"calibration_data","update-settings.json")),"Update settings lost");
  Assert(File.Exists(Path.Combine(stage,"calibration_data","update-github-token.dpapi")),"Encrypted credential lost");
  Assert(!Directory.Exists(Path.Combine(stage,"calibration_data","updates")),"Download cache copied");
  Assert(!File.Exists(Path.Combine(stage,"calibration_data",".station-running.lock")),"Station lock copied");
  string fullStage=Path.Combine(programs,"Leanwatts-Testbench-Gen2.install-fullcheck");
  Extract(args[3],fullStage);
  Assert(File.ReadAllText(Path.Combine(fullStage,"app.py"))=="new application", "Full payload path regressed");
  File.WriteAllText(Path.Combine(target,"app.py"),"modified installed app");
  bool rejected=false;
  try { Extract(args[2],Path.Combine(programs,"Leanwatts-Testbench-Gen2.install-reject")); }
  catch(TargetInvocationException ex) { rejected=ex.InnerException is InvalidDataException; }
  Assert(rejected,"Modified installed base was accepted");
  Assert(File.ReadAllText(Path.Combine(target,"app.py"))=="modified installed app","Rejected update changed existing app");
  File.WriteAllText(Path.Combine(target,"app.py"),"old application");
  string versionPath=Path.Combine(target,"VERSION.json");
  string oldVersion=File.ReadAllText(versionPath);
  File.WriteAllText(versionPath,oldVersion.Replace("0.0","9.9"));
  rejected=false;
  try { installer.GetMethod("RejectInstalledDowngrade",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[0]); }
  catch(TargetInvocationException ex) { rejected=ex.InnerException is InvalidDataException; }
  Assert(rejected,"Full installer downgrade guard accepted newer installed version");
  rejected=false;
  try { Extract(args[2],Path.Combine(programs,"Leanwatts-Testbench-Gen2.install-wrongversion")); }
  catch(TargetInvocationException ex) { rejected=ex.InnerException is InvalidDataException; }
  Assert(rejected,"Mismatched installed version was accepted");
  File.WriteAllText(versionPath,oldVersion);
  installer.GetMethod("RejectInstalledDowngrade",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[0]);
  File.WriteAllText(versionPath,oldVersion.Replace("0.0","0.0.1"));
  installer.GetMethod("RejectInstalledDowngrade",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[0]);
  File.WriteAllText(versionPath,oldVersion);
  string manifestPath=Path.Combine(target,"MANIFEST.json");
  string oldManifest=File.ReadAllText(manifestPath);
  File.WriteAllText(manifestPath,oldManifest+" ");
  rejected=false;
  try { Extract(args[2],Path.Combine(programs,"Leanwatts-Testbench-Gen2.install-wrongmanifest")); }
  catch(TargetInvocationException ex) { rejected=ex.InnerException is InvalidDataException; }
  Assert(rejected,"Mismatched installed manifest was accepted");
  Console.WriteLine("C# staging checks passed: delta overlay, removal, exclusions, untouched data, full payload, modified-base rejection.");
 }
}'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=delta.ROOT / "artifacts/delta-synthetic-checks")
    root = parser.parse_args().output_dir.resolve()
    if root.name != "delta-synthetic-checks" or not root.is_relative_to((delta.ROOT / "artifacts").resolve()):
        raise SystemExit("Choose an artifacts subdirectory ending in delta-synthetic-checks.")
    if root.exists():
        raise SystemExit("Synthetic check artifacts already exist; preserving them.")
    root.mkdir(parents=True)
    base, target = root / "base-fixture.zip", root / "target-fixture.zip"
    runtime = {"runtime/python.exe": b"unchanged runtime fixture"}
    release(base, "0.0", dict(runtime, **{"app.py": b"old application", "unchanged.bin": b"same", "removed.txt": b"obsolete"}))
    release(target, "0.0.1", dict(runtime, **{"app.py": b"new application", "unchanged.bin": b"same", "added.txt": b"new file"}))
    report = delta.build(base, target, "0.0.1", root / "Fixture-Update.exe")
    assert set(report["changed_files"]) == {"VERSION.json", "app.py", "added.txt"}
    assert report["removed_files"] == ["removed.txt"]
    assert not report["installer_executed"] and not report["published"]
    corrupt_base = root / "corrupted-base-fixture.zip"
    with zipfile.ZipFile(base) as source, zipfile.ZipFile(corrupt_base, "x") as broken:
        for entry in source.infolist():
            broken.writestr(entry, b"corrupted" if entry.filename == delta.PREFIX + "app.py" else source.read(entry.filename))
    for action in (
        lambda: delta.build(base, target, "0.0.1", root / "Fixture-Update.exe"),
        lambda: delta.build(target, base, "0.0", root / "Downgrade.exe"),
        lambda: delta.build(base, target, "0.0.2", root / "Wrong-Version.exe"),
        lambda: delta.build(corrupt_base, target, "0.0.1", root / "Corrupt-Base.exe"),
        lambda: delta.safe_name("../outside.txt"),
        lambda: delta.safe_name(".. /outside.txt"),
        lambda: delta.safe_name("runtime/NUL.txt"),
        lambda: delta.safe_name("runtime//python.exe"),
        lambda: delta.safe_name("calibration_data/profiles.json"),
    ):
        try:
            action()
        except ValueError:
            pass
        else:
            raise AssertionError("Unsafe or inconsistent delta build accepted")
    installed = root / "Programs/Leanwatts-Testbench-Gen2"
    installed.mkdir(parents=True)
    with zipfile.ZipFile(base) as archive:
        for entry in archive.infolist():
            relative = entry.filename.removeprefix(delta.PREFIX)
            delta.safe_name(relative)
            path = installed / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(entry.filename))
    (installed / "calibration_data").mkdir()
    (installed / "calibration_data/profiles.json").write_text("station data")
    (installed / "calibration_data/update-settings.json").write_text("synthetic update settings")
    (installed / "calibration_data/update-github-token.dpapi").write_bytes(b"synthetic encrypted token")
    (installed / "calibration_data/.station-running.lock").write_text("stale lock")
    (installed / "calibration_data/updates").mkdir()
    (installed / "calibration_data/updates/cache.exe").write_bytes(b"synthetic cache")
    (installed / ".app-running.lock").write_text("not a running app")
    (installed / "untracked.txt").write_text("do not copy")
    harness = root / "StagingChecks.cs"
    harness.write_text(HARNESS, encoding="utf-8")
    executable = root / "StagingChecks.exe"
    subprocess.run([str(delta.CSC), "/nologo", "/target:exe", "/out:"+str(executable),
        "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll", str(harness)], check=True)
    subprocess.run([str(executable), str(root), report["artifact"], report["payload"], str(target)], check=True)
    evidence = dict(checks_passed=True, synthetic_versions_only=True,
        installer_main_invoked=False, installer_install_invoked=False,
        real_installation_modified=False, hardware_access=False,
        artifact_folder=str(root), delta_report=report)
    (root / "validation.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
