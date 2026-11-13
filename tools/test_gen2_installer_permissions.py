"""Exercise the actual installer against isolated Windows folders, never a station."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_gen2_delta as delta
from test_gen2_delta import release


HARNESS = r'''using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
class InstallerChecks {
 static Type installer;
 static string root, fixture;
 static int count;
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
 static extern SafeFileHandle CreateFile(string path,uint access,uint share,IntPtr security,uint mode,uint flags,IntPtr template);
 static void Set(string name,object value) { installer.GetField(name,BindingFlags.Static|BindingFlags.NonPublic).SetValue(null,value); }
 static void Assert(bool test,string message) { if(!test) throw new Exception(message); count++; }
 static string Prepare(string name) {
  string programs=Path.Combine(root,name,"Programs");
  string target=Path.Combine(programs,"Leanwatts-Testbench-Gen2");
  Directory.CreateDirectory(target);
  using(var zip=ZipFile.OpenRead(fixture)) foreach(var entry in zip.Entries) {
   string file=Path.Combine(target,entry.FullName.Substring("CAN-Calibration/".Length).Replace('/',Path.DirectorySeparatorChar));
   Directory.CreateDirectory(Path.GetDirectoryName(file)); entry.ExtractToFile(file);
  }
  Directory.CreateDirectory(Path.Combine(target,"calibration_data","updates"));
  File.WriteAllText(Path.Combine(target,"calibration_data","accounts.json"),"synthetic accounts");
  File.WriteAllText(Path.Combine(target,"calibration_data","recipes.json"),"synthetic recipes");
  File.WriteAllText(Path.Combine(target,"calibration_data","updates","keep.txt"),"synthetic cache");
  File.WriteAllText(Path.Combine(target,"untracked.txt"),"keep this user file");
  Set("Programs",programs); Set("Target",target);
  Set("Desktop",Path.Combine(root,name,"Desktop"));
  Set("StartMenu",Path.Combine(root,name,"StartMenu"));
  return target;
 }
 static string Install(Action<string> progress) {
  try { return (string)installer.GetMethod("Install",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,new object[]{"",progress}); }
  catch(TargetInvocationException ex) { throw ex.InnerException; }
 }
 static void CheckData(string target) {
  Assert(File.ReadAllText(Path.Combine(target,"calibration_data","accounts.json"))=="synthetic accounts","accounts changed");
  Assert(File.ReadAllText(Path.Combine(target,"calibration_data","recipes.json"))=="synthetic recipes","recipes changed");
  Assert(File.ReadAllText(Path.Combine(target,"calibration_data","updates","keep.txt"))=="synthetic cache","update cache changed");
  Assert(File.ReadAllText(Path.Combine(target,"untracked.txt"))=="keep this user file","untracked file changed");
 }
 static void CheckOld(string target) {
  Assert(File.ReadAllText(Path.Combine(target,"app.py"))=="old application","old app not restored");
  Assert(File.ReadAllText(Path.Combine(target,"z-last.txt"))=="old last file","old final file changed");
  Assert(File.ReadAllText(Path.Combine(target,"removed.txt"))=="obsolete","removed file not restored");
  Assert(!File.Exists(Path.Combine(target,"added.txt")),"new file left after failed install");
  Assert(File.ReadAllText(Path.Combine(target,"VERSION.json")).Contains("\"0.0\""),"old version changed");
  CheckData(target);
 }
 static void Main(string[] args) {
  root=Path.GetFullPath(args[0]); fixture=Path.GetFullPath(args[2]);
  if(!root.EndsWith("installer-permission-checks",StringComparison.Ordinal)) throw new Exception("Unexpected test root");
  installer=Assembly.LoadFile(Path.GetFullPath(args[1])).GetType("Installer",true);
  string target=Prepare("root-locked");
  string runtime=Path.Combine(target,"runtime","python.exe");
  File.SetAttributes(runtime,File.GetAttributes(runtime)|FileAttributes.ReadOnly);
  string previous=Environment.CurrentDirectory;
  string backup;
  using(var directory=CreateFile(target,1,3,IntPtr.Zero,3,0x02000000,IntPtr.Zero)) {
   Assert(!directory.IsInvalid,"could not open directory lock fixture");
   using(var heldRuntime=new FileStream(runtime,FileMode.Open,FileAccess.Read,FileShare.Read)) {
    Environment.CurrentDirectory=target;
    try {
     bool oldRenameBlocked=false;
     try { Directory.Move(target,target+"-unexpected-move"); } catch(IOException) { oldRenameBlocked=true; } catch(UnauthorizedAccessException) { oldRenameBlocked=true; }
     Assert(oldRenameBlocked,"fixture did not reproduce blocked folder replacement");
     backup=Install(s=>Console.WriteLine("progress: "+s));
    }
    finally { Environment.CurrentDirectory=previous; }
   }
  }
  Assert(Directory.Exists(backup),"recovery backup missing");
  Assert(File.ReadAllText(Path.Combine(target,"app.py"))=="new application","new app absent");
  Assert(File.ReadAllText(Path.Combine(target,"z-last.txt"))=="new last file","last file absent");
  Assert(File.Exists(Path.Combine(target,"added.txt")),"added file absent");
  Assert(!File.Exists(Path.Combine(target,"removed.txt")),"removed file remains");
  Assert(File.ReadAllText(Path.Combine(target,"VERSION.json")).Contains("\"0.0.1\""),"new version absent");
  Assert((File.GetAttributes(runtime)&FileAttributes.ReadOnly)!=0,"unchanged runtime attributes changed");
  Assert(File.ReadAllText(Path.Combine(backup,"files","app.py"))=="old application","old app backup absent");
  CheckData(target);
  Console.WriteLine("PASS: blocked root replacement, locked runtime, current directory, data preservation.");

  target=Prepare("locked-change");
  using(var held=new FileStream(Path.Combine(target,"app.py"),FileMode.Open,FileAccess.Read,FileShare.Read)) {
   bool failed=false; try { Install(s=>{}); } catch(IOException) { failed=true; } catch(UnauthorizedAccessException) { failed=true; }
   Assert(failed,"locked changed file was accepted");
  }
  CheckOld(target);
  Console.WriteLine("PASS: locked changed file rejected before mutation.");

  target=Prepare("rollback");
  FileStream lateLock=null; bool injected=false; bool rolledBack=false;
  try { Install(s=> {
   if(s=="Applying application file: z-last.txt") { lateLock=new FileStream(Path.Combine(target,"z-last.txt"),FileMode.Open,FileAccess.Read,FileShare.Read); injected=true; }
   if(s.StartsWith("Restoring application file:") && lateLock!=null) { lateLock.Dispose(); lateLock=null; }
  }); } catch(IOException) { rolledBack=true; } catch(UnauthorizedAccessException) { rolledBack=true; }
  finally { if(lateLock!=null) lateLock.Dispose(); }
  Assert(injected,"late failure was not injected"); Assert(rolledBack,"late failure did not reject update");
  CheckOld(target);
  Console.WriteLine("PASS: later write failure restores original files and manifest.");

  target=Prepare("partial-replace");
  bool damaged=false, partialRejected=false;
  try { Install(s=> {
   if(s=="Applying application file: z-last.txt") { File.Delete(Path.Combine(target,"z-last.txt")); damaged=true; }
  }); } catch(IOException) { partialRejected=true; }
  Assert(damaged && partialRejected,"partial replacement failure was not exercised");
  CheckOld(target);
  Console.WriteLine("PASS: missing destination from failed replacement is restored.");

  target=Prepare("late-new-collision");
  bool collided=false, collisionRejected=false;
  try { Install(s=> {
   if(s=="Applying application file: added.txt") { File.WriteAllText(Path.Combine(target,"added.txt"),"late user file"); collided=true; }
  }); } catch(IOException ex) { collisionRejected=true; Console.WriteLine("Injected collision: "+ex.Message); }
  Assert(collided && collisionRejected,"late untracked collision was not rejected");
  Assert(File.ReadAllText(Path.Combine(target,"added.txt"))=="late user file","rollback deleted untracked collision");
  Assert(File.ReadAllText(Path.Combine(target,"app.py"))=="old application","collision changed old application");
  Assert(File.ReadAllText(Path.Combine(target,"VERSION.json")).Contains("\"0.0\""),"collision did not restore version");
  CheckData(target);
  Console.WriteLine("PASS: late untracked file collision preserved on rollback.");

  target=Prepare("cleanup-failure");
  FileStream heldStage=null; string cleanupError="";
  try { Install(s=> {
   if(s=="Applying application file: app.py") {
    string stage=Directory.GetDirectories(Path.GetDirectoryName(target),"Leanwatts-Testbench-Gen2.install-*")[0];
    heldStage=new FileStream(Path.Combine(stage,"unchanged.bin"),FileMode.Open,FileAccess.Read,FileShare.Read);
    throw new IOException("ORIGINAL INJECTED FAILURE");
   }
  }); } catch(IOException ex) { cleanupError=ex.Message; }
  finally { if(heldStage!=null) heldStage.Dispose(); }
  Assert(cleanupError.Contains("ORIGINAL INJECTED FAILURE"),"cleanup masked original failure");
  Assert(((string)installer.GetField("LastWarning",BindingFlags.Static|BindingFlags.NonPublic).GetValue(null)).Contains("Temporary files remain"),"cleanup warning missing");
  CheckOld(target);
  Console.WriteLine("PASS: cleanup failure retains original error and restored application.");

  target=Prepare("read-only-change");
  string readOnlyFile=Path.Combine(target,"app.py");
  File.SetAttributes(readOnlyFile,File.GetAttributes(readOnlyFile)|FileAttributes.ReadOnly);
  bool readOnlyRejected=false;
  try { Install(s=>{}); } catch(IOException) { readOnlyRejected=true; } catch(UnauthorizedAccessException) { readOnlyRejected=true; }
  Assert(readOnlyRejected,"read-only changed file should be reported without changing permissions");
  Assert((File.GetAttributes(readOnlyFile)&FileAttributes.ReadOnly)!=0,"original read-only bit changed");
  CheckOld(target);
  Console.WriteLine("PASS: read-only target reported; installed attributes/data preserved.");

  target=Prepare("shortcut-denied");
  string desktop=Path.Combine(root,"shortcut-denied","Desktop"); Directory.CreateDirectory(desktop);
  string link=Path.Combine(desktop,"Leanwatts Testbench Gen2.lnk"); File.WriteAllText(link,"locked shortcut fixture");
  using(var held=new FileStream(link,FileMode.Open,FileAccess.Read,FileShare.Read)) { Install(s=>{}); }
  string warning=(string)installer.GetField("LastWarning",BindingFlags.Static|BindingFlags.NonPublic).GetValue(null);
  Assert(!String.IsNullOrWhiteSpace(warning),"shortcut failure warning missing");
  Assert(File.ReadAllText(Path.Combine(target,"app.py"))=="new application","shortcut failure prevented committed update");
  CheckData(target);
  Console.WriteLine("PASS: shortcut failure does not misreport committed update.");

  target=Prepare("modified-base"); File.WriteAllText(Path.Combine(target,"app.py"),"locally modified");
  bool rejected=false; try { Install(s=>{}); } catch(IOException) { rejected=true; } catch(InvalidDataException) { rejected=true; }
  Assert(rejected,"modified base was accepted");
  Assert(File.ReadAllText(Path.Combine(target,"app.py"))=="locally modified","modified source overwritten");
  CheckData(target);
  Console.WriteLine("PASS: modified base rejected. Assertions: "+count);
 }
}'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    root = parser.parse_args().output_dir.resolve()
    if root.name != "installer-permission-checks" or not root.is_relative_to((delta.ROOT / "artifacts").resolve()):
        raise SystemExit("Use an artifacts child ending in installer-permission-checks.")
    root.mkdir(parents=True, exist_ok=False)
    base, target = root / "base.zip", root / "target.zip"
    common = {"runtime/python.exe": b"unchanged runtime fixture", "unchanged.bin": b"same"}
    release(base, "0.0", dict(common, **{"app.py": b"old application", "removed.txt": b"obsolete", "z-last.txt": b"old last file"}))
    release(target, "0.0.1", dict(common, **{"app.py": b"new application", "added.txt": b"new file", "z-last.txt": b"new last file"}))
    report = delta.build(base, target, "0.0.1", root / "Fixture-Update.exe")
    harness = root / "InstallerChecks.cs"
    harness.write_text(HARNESS, encoding="utf-8")
    executable = root / "InstallerChecks.exe"
    subprocess.run([str(delta.CSC), "/nologo", "/target:exe", "/out:" + str(executable),
                    "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll", str(harness)], check=True)
    result = subprocess.run([str(executable), str(root), report["artifact"], str(base)], capture_output=True, text=True)
    if result.returncode:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    evidence = dict(passed=True, actual_install_method_executed=True, synthetic_roots_only=True,
                    installed_station_modified=False, hardware_access=False, stdout=result.stdout,
                    stderr=result.stderr, installer_sha256=report["sha256"])
    (root / "validation.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
