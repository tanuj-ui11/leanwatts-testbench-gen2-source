// Per-user installer. It never accesses instruments, installs drivers, or changes Windows protections.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("Leanwatts Testbench Gen2 Setup")]
[assembly: AssemblyDescription("Leanwatts Testbench Gen2 1.1 workstation installer")]
[assembly: AssemblyVersion("1.1.0.0")]
[assembly: AssemblyFileVersion("1.1.0.0")]

internal static class Installer
{
    internal const string Version = "1.1";
    // Replaced by the packaging tool with the exact embedded payload digest.
    internal const string PayloadHash = "__PAYLOAD_SHA256__";
    internal static readonly string Programs = Path.GetFullPath(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs"));
    internal static readonly string Target = Path.Combine(Programs, "Leanwatts-Testbench-Gen2");
    internal static readonly string Desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
    internal static readonly string StartMenu = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
    internal static string LastWarning = "";

    [STAThread]
    private static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try { if (PrepareLaunch(args)) return; }
        catch (Exception ex) { MessageBox.Show("Could not prepare the installer: " + ex.Message); return; }
        foreach (string waitArgument in new string[] { "--wait-installer-pid", "--wait-pid" })
        {
        int at = Array.IndexOf(args, waitArgument);
        int pid;
        if (at >= 0 && at + 1 < args.Length && Int32.TryParse(args[at + 1], out pid))
        {
            try
            {
                using (Process process = Process.GetProcessById(pid))
                {
                    if (!process.WaitForExit(30000))
                    {
                        MessageBox.Show("Close Leanwatts Testbench Gen2, then open this installer again.", "Leanwatts Testbench Gen2", MessageBoxButtons.OK, MessageBoxIcon.Information);
                        return;
                    }
                }
            }
            catch (ArgumentException) { }
            catch (Exception ex) { MessageBox.Show("Could not wait for the application to close: " + ex.Message); return; }
        }
        }
        bool created;
        using (Mutex mutex = new Mutex(true, "Local\\LeanwattsTestbenchGen2Installer", out created))
        {
            if (!created) { MessageBox.Show("Another Leanwatts Testbench Gen2 installer is already open."); return; }
            Application.Run(new SetupForm());
        }
    }

    private static bool PrepareLaunch(string[] args)
    {
        // Do not retain a current-directory handle inside the application root.
        string temporary = Path.GetFullPath(Path.GetTempPath());
        if (temporary.StartsWith(Target.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase)
            || String.Equals(temporary.TrimEnd('\\'), Target.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            throw new IOException("The Windows temporary folder is inside the application. Choose a normal temporary folder before updating.");
        Directory.SetCurrentDirectory(temporary);
        string executable = Path.GetFullPath(Assembly.GetExecutingAssembly().Location);
        if (!executable.StartsWith(Target.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase)) return false;
        string stage = Path.Combine(temporary, "Leanwatts-Testbench-Gen2-Setup-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(stage);
        string copy = Path.Combine(stage, Path.GetFileName(executable));
        File.Copy(executable, copy, false);
        using (FileStream original = File.OpenRead(executable))
        using (FileStream copied = File.OpenRead(copy))
            if (Hash(original) != Hash(copied)) throw new IOException("The temporary installer copy failed verification.");
        string arguments = "--wait-installer-pid " + Process.GetCurrentProcess().Id;
        int at = Array.IndexOf(args, "--wait-pid");
        int parent;
        if (at >= 0 && at + 1 < args.Length && Int32.TryParse(args[at + 1], out parent) && parent > 0)
            arguments += " --wait-pid " + parent;
        Process.Start(new ProcessStartInfo(copy, arguments) { WorkingDirectory = stage, UseShellExecute = false });
        return true;
    }

    private static string Hash(Stream stream)
    {
        using (SHA256 sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static string SafeChild(string root, string relative)
    {
        if (String.IsNullOrEmpty(relative) || Path.IsPathRooted(relative) || relative.Contains(":") || relative.Contains("\\"))
            throw new InvalidDataException("Unsafe package filename.");
        foreach (string part in relative.Split('/'))
        {
            string stem = part.Split('.')[0].ToUpperInvariant();
            bool device = stem == "CON" || stem == "PRN" || stem == "AUX" || stem == "NUL"
                || (stem.Length == 4 && (stem.StartsWith("COM") || stem.StartsWith("LPT")) && stem[3] >= '0' && stem[3] <= '9');
            if (part.Length == 0 || part == ".." || part == "." || part != part.TrimEnd(' ', '.') || device
                || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                throw new InvalidDataException("Unsafe package filename.");
        }
        string full = Path.GetFullPath(Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(Path.GetFullPath(root).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Package path leaves its installation directory.");
        return full;
    }

    private static void OwnedDirectory(string path, string prefix)
    {
        string full = Path.GetFullPath(path);
        if (!String.Equals(Path.GetDirectoryName(full), Programs, StringComparison.OrdinalIgnoreCase)
            || !Path.GetFileName(full).StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Installer directory is outside the expected Programs location.");
    }

    private static void NoLinks(string root, string filename)
    {
        string normalized = Path.GetFullPath(root).TrimEnd('\\');
        string full = Path.GetFullPath(filename);
        if (!String.Equals(full, normalized, StringComparison.OrdinalIgnoreCase)
            && !full.StartsWith(normalized + "\\", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Application path leaves the verified installation directory.");
        string current = normalized;
        if ((Directory.Exists(current) || File.Exists(current))
            && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("The installation contains a linked directory; use a normal installation folder.");
        string relative = full.Substring(normalized.Length).TrimStart('\\');
        foreach (string part in relative.Split(new char[] { '\\' }, StringSplitOptions.RemoveEmptyEntries))
        {
            current = Path.Combine(current, part);
            if ((Directory.Exists(current) || File.Exists(current))
                && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("The installation contains a linked application file or directory.");
        }
    }

    private static Dictionary<string, object> ReadJson(string text)
    {
        JavaScriptSerializer json = new JavaScriptSerializer();
        json.MaxJsonLength = 16 * 1024 * 1024;
        Dictionary<string, object> result = json.Deserialize<Dictionary<string, object>>(text);
        if (result == null) throw new InvalidDataException("Package metadata is empty.");
        return result;
    }

    private static string EntryText(ZipArchiveEntry entry)
    {
        if (entry == null || entry.Length > 16 * 1024 * 1024) throw new InvalidDataException("Package metadata is missing or too large.");
        using (StreamReader reader = new StreamReader(entry.Open(), Encoding.UTF8)) return reader.ReadToEnd();
    }

    private static string TextHash(string text)
    {
        using (MemoryStream stream = new MemoryStream(Encoding.UTF8.GetBytes(text))) return Hash(stream);
    }

    private static bool ReservedApplicationPath(string relative)
    {
        string lower = relative.ToLowerInvariant();
        string first = lower.Split('/')[0];
        return first == "calibration_data" || first == "updates" || first == ".app-running.lock"
            || first == ".station-running.lock" || lower.EndsWith("/.app-running.lock")
            || lower.EndsWith("/.station-running.lock");
    }

    private static void VerifyManifest(string root, Dictionary<string, object> manifest)
    {
        HashSet<string> names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (KeyValuePair<string, object> item in manifest)
        {
            if (!names.Add(item.Key) || ReservedApplicationPath(item.Key) || String.Equals(item.Key, "MANIFEST.json", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Unsafe or duplicate application manifest entry.");
            string filename = SafeChild(root, item.Key);
            NoLinks(root, filename);
            Dictionary<string, object> expected = item.Value as Dictionary<string, object>;
            if (expected == null || !expected.ContainsKey("bytes") || !expected.ContainsKey("sha256"))
                throw new InvalidDataException("Invalid file manifest entry: " + item.Key);
            if (!File.Exists(filename) || new FileInfo(filename).Length != Convert.ToInt64(expected["bytes"]))
                throw new InvalidDataException("Application size mismatch: " + item.Key + ". Use the full installer if the installed application was modified.");
            using (FileStream file = File.OpenRead(filename))
                if (Hash(file) != (string)expected["sha256"])
                    throw new InvalidDataException("Application checksum mismatch: " + item.Key + ". Use the full installer if the installed application was modified.");
        }
    }

    private static void VerifyVersion(string root, string expected)
    {
        string path = SafeChild(root, "VERSION.json");
        NoLinks(root, path);
        if (!File.Exists(path)) throw new InvalidDataException("The installed version is unknown. Use the full installer first.");
        Dictionary<string, object> value = ReadJson(File.ReadAllText(path, Encoding.UTF8));
        if (!value.ContainsKey("application") || (string)value["application"] != "leanwatts-testbench-gen2"
            || !value.ContainsKey("version") || (string)value["version"] != expected)
            throw new InvalidDataException("This update requires installed Leanwatts Testbench Gen2 " + expected + ". Use the matching update or the full installer.");
    }

    private static System.Version NumericVersion(string text)
    {
        System.Version parsed;
        if (!System.Version.TryParse(text, out parsed)) throw new InvalidDataException("Invalid package version.");
        return new System.Version(parsed.Major, parsed.Minor, Math.Max(0, parsed.Build), Math.Max(0, parsed.Revision));
    }

    private static void RejectInstalledDowngrade()
    {
        string filename = SafeChild(Target, "VERSION.json");
        NoLinks(Programs, Target);
        NoLinks(Target, filename);
        if (!File.Exists(filename)) return; // First installation / legacy data import.
        Dictionary<string, object> installed = ReadJson(File.ReadAllText(filename, Encoding.UTF8));
        if (!installed.ContainsKey("application") || (string)installed["application"] != "leanwatts-testbench-gen2") return;
        if (!installed.ContainsKey("version") || !(installed["version"] is string))
            throw new InvalidDataException("The installed Gen2 version is unreadable; its files were preserved.");
        string current = (string)installed["version"];
        if (NumericVersion(current).CompareTo(NumericVersion(Version)) > 0)
            throw new InvalidDataException("Installed Leanwatts Testbench Gen2 " + current + " is newer than this installer (" + Version + "). Downgrades are not allowed.");
    }

    private static void ExtractPackage(ZipArchive archive, string stage, Action<string> progress)
    {
        const string prefix = "CAN-Calibration/";
        string targetManifestText = EntryText(archive.GetEntry(prefix + "MANIFEST.json"));
        Dictionary<string, object> manifest = ReadJson(targetManifestText);
        ZipArchiveEntry packageEntry = archive.GetEntry("PACKAGE.json");
        bool delta = packageEntry != null;
        if (delta)
        {
            Dictionary<string, object> package = ReadJson(EntryText(packageEntry));
            if (Convert.ToInt32(package["schema"]) != 1 || (string)package["application"] != "leanwatts-testbench-gen2"
                || (string)package["package_type"] != "delta" || (string)package["version"] != Version)
                throw new InvalidDataException("The incremental update metadata does not match this installer.");
            string baseVersion = (string)package["base_version"];
            if (NumericVersion(Version).CompareTo(NumericVersion(baseVersion)) <= 0)
                throw new InvalidDataException("An incremental update must have a newer target version.");
            if (!Directory.Exists(Target))
                throw new InvalidDataException("Install Leanwatts Testbench Gen2 " + baseVersion + " with the full installer first. Incremental updates only support the installed desktop application.");
            OwnedDirectory(Target, "Leanwatts-Testbench-Gen2");
            NoLinks(Programs, Target);
            string baseManifestText = EntryText(archive.GetEntry("BASE-MANIFEST.json"));
            if (TextHash(baseManifestText) != (string)package["base_manifest_sha256"]
                || TextHash(targetManifestText) != (string)package["target_manifest_sha256"])
                throw new InvalidDataException("Incremental update manifest hashes do not match.");
            Dictionary<string, object> baseline = ReadJson(baseManifestText);
            VerifyVersion(Target, baseVersion);
            string installedManifest = SafeChild(Target, "MANIFEST.json");
            NoLinks(Target, installedManifest);
            if (!File.Exists(installedManifest) || TextHash(File.ReadAllText(installedManifest, Encoding.UTF8)) != (string)package["base_manifest_sha256"])
                throw new InvalidDataException("Installed application manifest differs from this update's base. Use the full installer.");
            progress("Verifying installed version " + baseVersion + " before updating…");
            VerifyManifest(Target, baseline);
            progress("Preparing the existing application and changed files…");
            foreach (KeyValuePair<string, object> item in baseline)
            {
                // Copy only tracked application files which remain in the target.
                // Station data, locks, cache and untracked files never enter this copy.
                if (!manifest.ContainsKey(item.Key)) continue;
                string source = SafeChild(Target, item.Key);
                string destination = SafeChild(stage, item.Key);
                NoLinks(Target, source);
                NoLinks(stage, destination);
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                File.Copy(source, destination, false);
                // Only our staging copy is writable; never alter installed attributes or ACLs.
                File.SetAttributes(destination, File.GetAttributes(destination) & ~FileAttributes.ReadOnly);
            }
        }
        progress((delta ? "Updating" : "Installing") + " Leanwatts Testbench Gen2 " + Version + "…");
        HashSet<string> extracted = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (ZipArchiveEntry entry in archive.Entries)
        {
            if (!extracted.Add(entry.FullName)) throw new InvalidDataException("Duplicate package filename.");
            if (delta && (entry.FullName == "PACKAGE.json" || entry.FullName == "BASE-MANIFEST.json")) continue;
            if (!entry.FullName.StartsWith(prefix, StringComparison.Ordinal)) throw new InvalidDataException("Unexpected package root.");
            string relative = entry.FullName.Substring(prefix.Length);
            if (relative.Length == 0 || relative.EndsWith("/")) continue;
            if (ReservedApplicationPath(relative)) throw new InvalidDataException("Installer unexpectedly contains station data, locks or update cache.");
            if (relative != "MANIFEST.json" && !manifest.ContainsKey(relative)) throw new InvalidDataException("Unlisted package file: " + relative);
            string output = SafeChild(stage, relative);
            NoLinks(stage, output);
            Directory.CreateDirectory(Path.GetDirectoryName(output));
            using (Stream input = entry.Open())
            using (FileStream file = new FileStream(output, delta ? FileMode.Create : FileMode.CreateNew, FileAccess.Write)) input.CopyTo(file);
        }
        VerifyManifest(stage, manifest);
        VerifyVersion(stage, Version);
    }

    private static void CopyData(string source, string destination, bool topLevel = true)
    {
        if (!Directory.Exists(source)) return;
        if ((File.GetAttributes(source) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Choose a normal data folder, not a symbolic link.");
        Directory.CreateDirectory(destination);
        foreach (string file in Directory.GetFiles(source))
        {
            string name = Path.GetFileName(file);
            if (name == ".app-running.lock" || name == ".station-running.lock") continue;
            if ((File.GetAttributes(file) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Saved data contains a linked file; import it manually after installation.");
            File.Copy(file, Path.Combine(destination, Path.GetFileName(file)), false);
        }
        foreach (string directory in Directory.GetDirectories(source))
        {
            if (topLevel && String.Equals(Path.GetFileName(directory), "updates", StringComparison.OrdinalIgnoreCase)) continue;
            CopyData(directory, Path.Combine(destination, Path.GetFileName(directory)), false);
        }
    }

    internal static bool IsRunning()
    {
        foreach (Process process in Process.GetProcesses())
        {
            using (process)
            {
                if (process.Id == Process.GetCurrentProcess().Id) continue;
                try
                {
                    string filename = process.MainModule.FileName;
                    if (filename.StartsWith(Target.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase)) return true;
                }
                catch (System.ComponentModel.Win32Exception) { }
                catch (InvalidOperationException) { }
            }
        }
        string lockFile = Path.Combine(Target, ".app-running.lock");
        if (File.Exists(lockFile))
        {
            try
            {
                int pid;
                if (Int32.TryParse(File.ReadAllLines(lockFile)[0], out pid))
                    using (Process process = Process.GetProcessById(pid)) { if (!process.HasExited) return true; }
            }
            catch (ArgumentException) { }
            catch (IOException) { return true; }
            catch (IndexOutOfRangeException) { }
        }
        return false;
    }

    private sealed class DeltaFile
    {
        internal string Relative, Destination, Saved, Incoming;
        internal bool Existed, Remove, Created;
        internal FileAttributes Attributes;
    }

    private static bool SameManifestFile(object first, object second)
    {
        Dictionary<string, object> a = first as Dictionary<string, object>;
        Dictionary<string, object> b = second as Dictionary<string, object>;
        return a != null && b != null && Convert.ToInt64(a["bytes"]) == Convert.ToInt64(b["bytes"])
            && String.Equals((string)a["sha256"], (string)b["sha256"], StringComparison.Ordinal);
    }

    private static void WriteRecovery(string backup, List<DeltaFile> files, string status, int next)
    {
        List<Dictionary<string, object>> entries = new List<Dictionary<string, object>>();
        foreach (DeltaFile item in files)
            entries.Add(new Dictionary<string, object> {
                { "path", item.Relative }, { "existed", item.Existed }, { "remove", item.Remove },
                { "original_attributes", (int)item.Attributes }
            });
        JavaScriptSerializer json = new JavaScriptSerializer();
        json.MaxJsonLength = 16 * 1024 * 1024;
        string text = json.Serialize(new Dictionary<string, object> {
            { "schema", 1 }, { "application", "leanwatts-testbench-gen2" }, { "version", Version },
            { "target", Target }, { "status", status }, { "next_operation", next }, { "files", entries }
        });
        string path = SafeChild(backup, "recovery.json");
        string temporary = SafeChild(backup, "recovery-next.json");
        NoLinks(backup, path); NoLinks(backup, temporary);
        File.WriteAllText(temporary, text, new UTF8Encoding(false));
        if (File.Exists(path)) File.Replace(temporary, path, null);
        else File.Move(temporary, path);
    }

    private static string CommitDelta(string stage, string backup, string baseManifestText, Action<string> progress)
    {
        OwnedDirectory(stage, "Leanwatts-Testbench-Gen2.install-");
        OwnedDirectory(backup, "Leanwatts-Testbench-Gen2.backup-");
        NoLinks(Programs, stage); NoLinks(Programs, backup); NoLinks(Programs, Target);
        Dictionary<string, object> baseline = ReadJson(baseManifestText);
        string stagedManifest = SafeChild(stage, "MANIFEST.json");
        NoLinks(stage, stagedManifest);
        string targetManifestText = File.ReadAllText(stagedManifest, Encoding.UTF8);
        Dictionary<string, object> manifest = ReadJson(targetManifestText);
        // Recheck both complete images immediately before touching any installed file.
        VerifyManifest(stage, manifest); VerifyVersion(stage, Version);
        VerifyManifest(Target, baseline);
        string installedManifest = SafeChild(Target, "MANIFEST.json");
        NoLinks(Target, installedManifest);
        if (TextHash(File.ReadAllText(installedManifest, Encoding.UTF8)) != TextHash(baseManifestText))
            throw new InvalidDataException("Installed manifest changed while preparing the update; no application files were changed.");
        List<string> names = new List<string>();
        HashSet<string> casing = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string name in baseline.Keys) casing.Add(name);
        foreach (KeyValuePair<string, object> entry in manifest)
        {
            if (!baseline.ContainsKey(entry.Key) && !casing.Add(entry.Key))
                throw new InvalidDataException("Case-only application renames are not supported: " + entry.Key);
            if (!baseline.ContainsKey(entry.Key) || !SameManifestFile(baseline[entry.Key], entry.Value)) names.Add(entry.Key);
        }
        foreach (string name in baseline.Keys) if (!manifest.ContainsKey(name)) names.Add(name);
        names.Sort(StringComparer.Ordinal);
        names.Add("MANIFEST.json"); // This identifies the installed version only after all files verify.
        List<DeltaFile> files = new List<DeltaFile>();
        foreach (string name in names)
        {
            DeltaFile item = new DeltaFile {
                Relative = name, Destination = SafeChild(Target, name), Saved = SafeChild(backup, "files/" + name),
                Incoming = SafeChild(stage, name), Remove = name != "MANIFEST.json" && !manifest.ContainsKey(name),
                Existed = name == "MANIFEST.json" || baseline.ContainsKey(name)
            };
            NoLinks(Target, item.Destination); NoLinks(stage, item.Incoming);
            if (Directory.Exists(item.Destination) || (!item.Existed && File.Exists(item.Destination)))
                throw new IOException("An untracked file or directory occupies the new application path: " + item.Destination);
            if (item.Existed)
            {
                item.Attributes = File.GetAttributes(item.Destination);
                if ((item.Attributes & FileAttributes.ReadOnly) != 0)
                    throw new IOException("The application file is read-only: " + item.Destination + ". Its attributes were not changed.");
                try { using (FileStream exclusive = new FileStream(item.Destination, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { } }
                catch (Exception ex) { throw new IOException("Cannot obtain exclusive write access to application file: " + item.Destination + ". Close the application and retry. " + ex.Message, ex); }
            }
            files.Add(item);
        }
        if (Directory.Exists(backup) || File.Exists(backup)) throw new IOException("The recovery backup already exists: " + backup);
        Directory.CreateDirectory(backup);
        progress("Backing up changed application files…");
        foreach (DeltaFile item in files)
        {
            if (!item.Existed) continue;
            NoLinks(Target, item.Destination); NoLinks(backup, item.Saved);
            Directory.CreateDirectory(Path.GetDirectoryName(item.Saved));
            File.Copy(item.Destination, item.Saved, false);
            using (FileStream original = File.OpenRead(item.Destination))
            using (FileStream saved = File.OpenRead(item.Saved))
                if (Hash(original) != Hash(saved)) throw new IOException("Backup verification failed: " + item.Destination);
        }
        File.WriteAllText(SafeChild(backup, "BASE-MANIFEST.json"), baseManifestText, new UTF8Encoding(false));
        File.WriteAllText(SafeChild(backup, "TARGET-MANIFEST.json"), targetManifestText, new UTF8Encoding(false));
        WriteRecovery(backup, files, "prepared", 0);
        List<DeltaFile> applied = new List<DeltaFile>();
        string current = Target;
        try
        {
            VerifyManifest(Target, baseline);
            if (TextHash(File.ReadAllText(installedManifest, Encoding.UTF8)) != TextHash(baseManifestText))
                throw new IOException("The installed manifest changed before applying the update.");
            if (IsRunning()) throw new IOException("The application reopened before the update; close it and retry.");
            for (int index = 0; index < files.Count; index++)
            {
                DeltaFile item = files[index]; current = item.Destination;
                progress("Applying application file: " + item.Relative);
                NoLinks(Target, item.Destination); NoLinks(stage, item.Incoming);
                WriteRecovery(backup, files, "applying", index);
                if (item.Relative == "MANIFEST.json") VerifyManifest(Target, manifest);
                Directory.CreateDirectory(Path.GetDirectoryName(item.Destination));
                // Windows ReplaceFile can change destination state on certain failures.
                // Record the attempt first; rollback skips an unchanged, possibly locked original.
                applied.Add(item);
                if (item.Remove) File.Delete(item.Destination);
                else if (item.Existed) File.Replace(item.Incoming, item.Destination, null);
                else { File.Move(item.Incoming, item.Destination); item.Created = true; }
            }
            VerifyManifest(Target, manifest); VerifyVersion(Target, Version);
            WriteRecovery(backup, files, "complete", files.Count);
            return backup;
        }
        catch (Exception failure)
        {
            List<string> restoreErrors = new List<string>();
            for (int index = applied.Count - 1; index >= 0; index--)
            {
                DeltaFile item = applied[index];
                try
                {
                    try { progress("Restoring application file: " + item.Relative); } catch { }
                    NoLinks(Target, item.Destination); NoLinks(backup, item.Saved);
                    if (!item.Existed)
                    {
                        // A failed Move may mean another file appeared after preflight.
                        // Never delete that untracked file during rollback.
                        if (item.Created)
                        {
                            if (File.Exists(item.Destination))
                                using (FileStream created = File.OpenRead(item.Destination))
                                    if (Hash(created) != (string)((Dictionary<string, object>)manifest[item.Relative])["sha256"])
                                        throw new IOException("New application file changed externally; preserved for recovery.");
                            File.Delete(item.Destination);
                        }
                    }
                    else
                    {
                        bool same = false;
                        if (File.Exists(item.Destination))
                            using (FileStream existing = File.OpenRead(item.Destination))
                            using (FileStream saved = File.OpenRead(item.Saved))
                                same = Hash(existing) == Hash(saved);
                        if (same)
                        {
                            if (File.GetAttributes(item.Destination) != item.Attributes)
                                File.SetAttributes(item.Destination, item.Attributes);
                            continue;
                        }
                        string incoming = SafeChild(backup, "restore-" + index + ".tmp");
                        File.Copy(item.Saved, incoming, false);
                        File.SetAttributes(incoming, File.GetAttributes(incoming) & ~FileAttributes.ReadOnly);
                        if (File.Exists(item.Destination)) File.Replace(incoming, item.Destination, null);
                        else File.Move(incoming, item.Destination);
                        File.SetAttributes(item.Destination, item.Attributes);
                    }
                }
                catch (Exception restore) { restoreErrors.Add(item.Relative + ": " + restore.Message); }
            }
            try
            {
                VerifyManifest(Target, baseline);
                if (TextHash(File.ReadAllText(installedManifest, Encoding.UTF8)) != TextHash(baseManifestText))
                    throw new IOException("The original manifest was not restored.");
            }
            catch (Exception verify) { restoreErrors.Add("Restoration verification: " + verify.Message); }
            try { WriteRecovery(backup, files, restoreErrors.Count == 0 ? "rolled_back" : "recovery_required", applied.Count); }
            catch (Exception ledger) { restoreErrors.Add("Recovery record: " + ledger.Message); }
            string recovery = restoreErrors.Count == 0 ? "The previous application was restored and verified."
                : "Recovery is incomplete; do not open the application. " + String.Join("; ", restoreErrors.ToArray());
            throw new IOException("Update failed while applying '" + current + "': " + failure.Message + " " + recovery + " Recovery backup: " + backup, failure);
        }
    }

    internal static string Install(string importData, Action<string> progress)
    {
        LastWarning = "";
        if (IsRunning()) throw new IOException("Close Leanwatts Testbench Gen2 before installing.");
        Directory.CreateDirectory(Programs);
        NoLinks(Programs, Programs);
        NoLinks(Programs, Target);
        RejectInstalledDowngrade();
        string stage = Path.Combine(Programs, "Leanwatts-Testbench-Gen2.install-" + Guid.NewGuid().ToString("N"));
        string rollback = Path.Combine(Programs, "Leanwatts-Testbench-Gen2.backup-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 6));
        OwnedDirectory(stage, "Leanwatts-Testbench-Gen2.install-");
        OwnedDirectory(rollback, "Leanwatts-Testbench-Gen2.backup-");
        bool movedOld = false;
        bool committed = false;
        bool delta = false;
        string baseManifestText = null;
        string phase = "preparing the temporary installation", phasePath = stage;
        try
        {
            Directory.CreateDirectory(stage);
            progress("Checking installer contents…");
            using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
            {
                if (payload == null || Hash(payload) != PayloadHash) throw new InvalidDataException("Installer payload failed its checksum check.");
                payload.Position = 0;
                using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read, true))
                {
                    delta = archive.GetEntry("PACKAGE.json") != null;
                    if (delta) baseManifestText = EntryText(archive.GetEntry("BASE-MANIFEST.json"));
                    phase = "verifying and staging application files";
                    ExtractPackage(archive, stage, progress);
                }
            }
            if (delta)
            {
                phase = "updating changed application files"; phasePath = Target;
                CommitDelta(stage, rollback, baseManifestText, progress);
                committed = true;
            }
            else
            {
            string existingData = Path.Combine(Target, "calibration_data");
            string dataSource = Directory.Exists(existingData) ? existingData : importData;
            if (!String.IsNullOrWhiteSpace(dataSource) && Directory.Exists(dataSource))
            {
                phase = "preserving station data"; phasePath = dataSource;
                progress("Preserving saved recipes, operators and session reports…");
                string stagedData = SafeChild(stage, "calibration_data");
                OwnedDirectory(stage, "Leanwatts-Testbench-Gen2.install-");
                NoLinks(Programs, stage);
                NoLinks(stage, stagedData);
                CopyData(Path.GetFullPath(dataSource), stagedData);
            }
            else Directory.CreateDirectory(Path.Combine(stage, "calibration_data"));
            if (IsRunning()) throw new IOException("Leanwatts Testbench Gen2 opened during installation. Close it and run Setup again.");
            if (Directory.Exists(Target))
            {
                phase = "backing up the previous application folder"; phasePath = Target;
                OwnedDirectory(Target, "Leanwatts-Testbench-Gen2");
                OwnedDirectory(rollback, "Leanwatts-Testbench-Gen2.backup-");
                NoLinks(Programs, Target);
                NoLinks(Programs, rollback);
                Directory.Move(Target, rollback);
                movedOld = true;
            }
            OwnedDirectory(stage, "Leanwatts-Testbench-Gen2.install-");
            OwnedDirectory(Target, "Leanwatts-Testbench-Gen2");
            NoLinks(Programs, stage);
            NoLinks(Programs, Target);
            phase = "placing the new application folder"; phasePath = Target;
            Directory.Move(stage, Target);
            committed = true;
            }
            try { progress("Creating the desktop shortcut…"); }
            catch (Exception display) { LastWarning += " Installation completed; progress display failed: " + display.Message; }
            foreach (string folder in new string[] { Desktop, StartMenu })
            {
                try { Shortcut(folder); }
                catch (Exception shortcut) { LastWarning += " Application installed, but the shortcut could not be saved in '" + folder + "': " + shortcut.Message; }
            }
            return movedOld || delta ? rollback : "";
        }
        catch (Exception failure)
        {
            if (movedOld && !committed && !Directory.Exists(Target) && Directory.Exists(rollback))
            {
                try
                {
                OwnedDirectory(rollback, "Leanwatts-Testbench-Gen2.backup-");
                OwnedDirectory(Target, "Leanwatts-Testbench-Gen2");
                NoLinks(Programs, rollback);
                NoLinks(Programs, Target);
                Directory.Move(rollback, Target);
                }
                catch (Exception restore)
                {
                    throw new IOException("Installation failed while " + phase + " at '" + phasePath + "': " + failure.Message
                        + ". Restoring the previous folder also failed: " + restore.Message + ". Recovery backup: " + rollback, failure);
                }
            }
            throw new IOException("Installation failed while " + phase + " at '" + phasePath + "': " + failure.Message, failure);
        }
        finally
        {
            if (Directory.Exists(stage))
            {
                try
                {
                OwnedDirectory(stage, "Leanwatts-Testbench-Gen2.install-");
                NoLinks(Programs, stage);
                RemoveStagingDirectory(stage, stage);
                }
                catch (Exception cleanup)
                {
                    // A cleanup failure must never replace the original installation error.
                    LastWarning += " Temporary files remain in '" + stage + "': " + cleanup.Message;
                }
            }
        }
    }

    private static void RemoveStagingDirectory(string root, string directory)
    {
        NoLinks(root, directory);
        foreach (string file in Directory.GetFiles(directory))
        {
            NoLinks(root, file);
            File.SetAttributes(file, File.GetAttributes(file) & ~FileAttributes.ReadOnly);
            File.Delete(file);
        }
        foreach (string child in Directory.GetDirectories(directory)) RemoveStagingDirectory(root, child);
        Directory.Delete(directory, false);
    }

    private static void Shortcut(string folder)
    {
        Directory.CreateDirectory(folder);
        Type shellType = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(shellType);
        object shortcut = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell,
                                                new object[] { Path.Combine(folder, "Leanwatts Testbench Gen2.lnk") });
        Type type = shortcut.GetType();
        type.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { Path.Combine(Target, "Leanwatts-Testbench-Gen2.exe") });
        type.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { Target });
        type.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { Path.Combine(Target, "Leanwatts-Testbench-Gen2.exe") + ",0" });
        type.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "Leanwatts Testbench Gen2 — operator sessions" });
        type.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, new object[0]);
        System.Runtime.InteropServices.Marshal.FinalReleaseComObject(shortcut);
        System.Runtime.InteropServices.Marshal.FinalReleaseComObject(shell);
    }

    private sealed class SetupForm : Form
    {
        private readonly TextBox source = new TextBox();
        private readonly Label status = new Label();
        private readonly Button install = new Button();
        private readonly Button browse = new Button();
        private readonly CheckBox launch = new CheckBox();
        private bool working;
        private bool installed;

        internal SetupForm()
        {
            Text = "Leanwatts Testbench Gen2 " + Version + " Setup";
            Width = 720; Height = 400; FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10);
            Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            using (Stream brand = Assembly.GetExecutingAssembly().GetManifestResourceStream("branding.png"))
            {
                if (brand != null)
                    using (Image sourceImage = Image.FromStream(brand))
                        Controls.Add(new PictureBox { Image = new Bitmap(sourceImage), Left = 24, Top = 14,
                                                     Width = 140, Height = 48, SizeMode = PictureBoxSizeMode.Zoom });
                else if (Icon != null)
                    Controls.Add(new PictureBox { Image = Icon.ToBitmap(), Left = 24, Top = 22, Width = 34,
                                                 Height = 34, SizeMode = PictureBoxSizeMode.Zoom });
            }
            Label heading = new Label { Text = "Testbench Gen2 " + Version, AutoSize = false,
                                        Left = 184, Top = 20, Width = 490, Height = 38, Font = new Font("Segoe UI", 19, FontStyle.Bold) };
            Controls.Add(heading);
            Controls.Add(new Label { Text = "One installation for this Windows account. A desktop shortcut is created.", Left = 26, Top = 66, Width = 620, Height = 25 });
            Controls.Add(new Label { Text = "Install location: " + Target, Left = 26, Top = 101, Width = 620, Height = 44 });
            Controls.Add(new Label { Text = "Existing calibration_data folder (optional, for the first upgrade):", Left = 26, Top = 149, Width = 620, Height = 25 });
            source.SetBounds(26, 178, 510, 28);
            browse.Text = "Browse…"; browse.SetBounds(546, 176, 95, 31);
            browse.Click += delegate {
                using (FolderBrowserDialog dialog = new FolderBrowserDialog())
                {
                    dialog.Description = "Choose the old calibration_data folder or its CAN-Calibration parent folder.";
                    if (dialog.ShowDialog(this) == DialogResult.OK)
                    {
                        string data = Path.Combine(dialog.SelectedPath, "calibration_data");
                        source.Text = Directory.Exists(data) ? data : dialog.SelectedPath;
                    }
                }
            };
            Controls.Add(source); Controls.Add(browse);
            string current = Path.Combine(Target, "calibration_data");
            if (Directory.Exists(current)) { source.Text = current; source.Enabled = browse.Enabled = false; }
            else
            {
                string latest = ""; DateTime latestTime = DateTime.MinValue;
                if (Directory.Exists(Programs)) foreach (string folder in Directory.GetDirectories(Programs, "CAN-Calibration*"))
                {
                    foreach (string data in new string[] { Path.Combine(folder, "calibration_data"), Path.Combine(folder, "CAN-Calibration", "calibration_data") })
                    {
                        if (Directory.Exists(data) && Directory.GetLastWriteTime(data) > latestTime)
                        { latest = data; latestTime = Directory.GetLastWriteTime(data); }
                    }
                }
                source.Text = latest;
            }
            status.SetBounds(26, 221, 615, 55);
            status.Text = "Close both charger applications before installing. Saved data is retained; existing installations get a backup.";
            Controls.Add(status);
            launch.Text = "Open Leanwatts Testbench Gen2"; launch.Checked = true; launch.SetBounds(26, 294, 390, 28);
            Controls.Add(launch);
            install.Text = "Install"; install.SetBounds(520, 286, 122, 39);
            install.Click += async delegate {
                if (installed) { if (launch.Checked) StartApplication(); Close(); return; }
                if (working) return;
                string data = source.Text.Trim();
                if (data.Length > 0 && !Directory.Exists(data)) { MessageBox.Show("The selected saved-data folder does not exist."); return; }
                if (data.Length > 0 && !String.Equals(Path.GetFileName(data.TrimEnd('\\')), "calibration_data", StringComparison.OrdinalIgnoreCase))
                { MessageBox.Show("Choose the calibration_data folder from the previous application."); return; }
                working = true; install.Enabled = browse.Enabled = source.Enabled = false;
                try
                {
                    string backup = await Task.Run(() => Install(data, message => BeginInvoke((Action)(() => status.Text = message))));
                    installed = true; install.Text = "Finish";
                    status.Text = "Installed successfully."
                        + (backup.Length > 0 ? " Recovery backup retained." : "") + LastWarning;
                }
                catch (Exception ex) { status.Text = "Installation could not finish: " + ex.Message; browse.Enabled = source.Enabled = true; }
                finally { working = false; install.Enabled = true; }
            };
            Controls.Add(install);
            FormClosing += delegate(object sender, FormClosingEventArgs e) { if (working) e.Cancel = true; };
        }

        private void StartApplication()
        {
            try { Process.Start(new ProcessStartInfo(Path.Combine(Target, "Leanwatts-Testbench-Gen2.exe")) { WorkingDirectory = Target, UseShellExecute = false }); }
            catch (Exception ex) { MessageBox.Show("Installation is complete, but the application could not open: " + ex.Message); }
        }
    }
}
