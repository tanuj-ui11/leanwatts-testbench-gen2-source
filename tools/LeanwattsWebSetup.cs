// Small public-release bootstrapper. No instrument access or credentials.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("Leanwatts Testbench Gen2 - Install Latest")]
[assembly: AssemblyDescription("Download and verify the latest full Leanwatts Testbench Gen2 installer")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

internal static class WebSetup
{
    internal const string Repository = "pradeep-lw/leanwatts-testbench-gen2";
    internal const string ApiRoot = "https://api.github.com/repos/" + Repository;
    internal const long MaxBytes = 512L * 1024 * 1024;
    internal const string NoReleaseMessage = "No public installer release is available yet. Version 1.1 is currently supplied locally. Use the offline Setup EXE, or try again after a release is published.";

    internal sealed class Release
    {
        internal string Version, Filename, Url, Sha256;
        internal long Size;
    }

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072; // TLS 1.2
        Application.Run(new SetupForm());
    }

    internal static Uri HttpsUri(string value)
    {
        Uri uri;
        if (String.IsNullOrWhiteSpace(value) || value.Length > 4096
            || !Uri.TryCreate(value, UriKind.Absolute, out uri) || uri.Scheme != Uri.UriSchemeHttps
            || uri.UserInfo.Length != 0 || uri.Fragment.Length != 0
            || (uri.Port != 443) || Regex.IsMatch(value, @"[\x00-\x20]"))
            throw new InvalidDataException("The release supplied an invalid HTTPS download address.");
        return uri;
    }

    private static HttpWebResponse Open(string address, bool binary, CancellationToken cancel)
    {
        Uri uri = HttpsUri(address);
        for (int redirects = 0; redirects <= 5; redirects++)
        {
            cancel.ThrowIfCancellationRequested();
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(uri);
            request.Method = "GET";
            request.AllowAutoRedirect = false;
            request.Timeout = 15000;
            request.ReadWriteTimeout = 15000;
            request.UserAgent = "Leanwatts-Testbench-Gen2-WebSetup/1.0";
            request.Accept = binary ? "application/octet-stream" : "application/vnd.github+json";
            if (uri.Host == "api.github.com") request.Headers["X-GitHub-Api-Version"] = "2026-03-10";
            HttpWebResponse response;
            using (cancel.Register(request.Abort))
            {
                try { response = (HttpWebResponse)request.GetResponse(); }
                catch (WebException ex)
                {
                    cancel.ThrowIfCancellationRequested();
                    using (HttpWebResponse error = ex.Response as HttpWebResponse)
                    {
                        if (error != null && error.StatusCode == HttpStatusCode.NotFound)
                            throw new InvalidOperationException(NoReleaseMessage);
                        if (error != null && (error.StatusCode == HttpStatusCode.Forbidden || (int)error.StatusCode == 429))
                            throw new InvalidOperationException("GitHub temporarily refused the download. Try again later, or use the offline Setup EXE.");
                    }
                    throw new IOException("The download server could not be reached securely. Check the Internet connection, or use the offline Setup EXE.");
                }
            }
            int code = (int)response.StatusCode;
            if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308)
            {
                string location = response.Headers["Location"];
                response.Dispose();
                if (String.IsNullOrWhiteSpace(location)) throw new InvalidDataException("The download redirect has no destination.");
                uri = HttpsUri(new Uri(uri, location).AbsoluteUri);
                continue;
            }
            if (response.StatusCode != HttpStatusCode.OK)
            { response.Dispose(); throw new IOException("The download server returned an unexpected response."); }
            HttpsUri(response.ResponseUri.AbsoluteUri);
            return response;
        }
        throw new IOException("The download followed too many redirects. Use the offline Setup EXE.");
    }

    internal static Dictionary<string, object> Json(string value)
    {
        JavaScriptSerializer serializer = new JavaScriptSerializer();
        serializer.MaxJsonLength = 2 * 1024 * 1024;
        Dictionary<string, object> result = serializer.Deserialize<Dictionary<string, object>>(value);
        if (result == null) throw new InvalidDataException("The release metadata is empty.");
        return result;
    }

    private static Dictionary<string, object> ReadJson(string address, int maximum, bool binary, CancellationToken cancel)
    {
        using (HttpWebResponse response = Open(address, binary, cancel))
        using (Stream input = response.GetResponseStream())
        using (MemoryStream output = new MemoryStream())
        {
            byte[] buffer = new byte[8192];
            int count;
            Stopwatch elapsed = Stopwatch.StartNew();
            while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
            {
                cancel.ThrowIfCancellationRequested();
                if (elapsed.Elapsed > TimeSpan.FromSeconds(60)) throw new IOException("The release lookup timed out. Try again or use the offline Setup EXE.");
                if (output.Length + count > maximum) throw new InvalidDataException("The release metadata is too large.");
                output.Write(buffer, 0, count);
            }
            return Json(Encoding.UTF8.GetString(output.ToArray()).TrimStart('\uFEFF'));
        }
    }

    private static object Value(Dictionary<string, object> value, string key)
    { object result; return value.TryGetValue(key, out result) ? result : null; }

    private static long Number(object value)
    {
        if (!(value is int) && !(value is long)) throw new InvalidDataException("The release contains an invalid numeric field.");
        return Convert.ToInt64(value);
    }

    private static List<Dictionary<string, object>> Objects(object value, int maximum)
    {
        IEnumerable array = value as IEnumerable;
        if (array == null || value is string || value is IDictionary) throw new InvalidDataException("The release contains an invalid list.");
        List<Dictionary<string, object>> result = new List<Dictionary<string, object>>();
        foreach (object item in array)
        {
            Dictionary<string, object> entry = item as Dictionary<string, object>;
            if (entry == null || result.Count >= maximum) throw new InvalidDataException("The release list is invalid or too large.");
            result.Add(entry);
        }
        return result;
    }

    private static string Filename(object value)
    {
        string name = value as string;
        if (name == null || !Regex.IsMatch(name, @"\A[A-Za-z0-9][A-Za-z0-9._ -]{0,180}\z")
            || name.EndsWith(".") || name.EndsWith(" ") || name.Contains(".."))
            throw new InvalidDataException("The release supplied an invalid filename.");
        string stem = name.Split('.')[0];
        if (Regex.IsMatch(stem, @"\A(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])\z", RegexOptions.IgnoreCase))
            throw new InvalidDataException("The release supplied a reserved Windows filename.");
        return name;
    }

    private static Dictionary<string, object> Asset(List<Dictionary<string, object>> assets, string name)
    {
        Dictionary<string, object> selected = null;
        foreach (Dictionary<string, object> asset in assets)
            if ((Value(asset, "name") as string) == name && (Value(asset, "state") as string) == "uploaded")
            {
                if (selected != null) throw new InvalidDataException("The release contains duplicate installer assets.");
                if (Number(Value(asset, "id")) <= 0) throw new InvalidDataException("The release asset ID is invalid.");
                selected = asset;
            }
        if (selected == null) throw new InvalidOperationException("The published release is missing " + name + ". Use the offline Setup EXE or ask for a complete release.");
        return selected;
    }

    internal static Release SelectFull(Dictionary<string, object> manifest, List<Dictionary<string, object>> assets)
    {
        if (Number(Value(manifest, "schema")) != 1 || (Value(manifest, "application") as string) != "leanwatts-testbench-gen2")
            throw new InvalidDataException("This release is not for Leanwatts Testbench Gen2.");
        string version = Value(manifest, "version") as string;
        if (version == null || !Regex.IsMatch(version, @"\A[0-9]{1,5}(\.[0-9]{1,5}){1,3}\z"))
            throw new InvalidDataException("The release version is invalid.");
        List<Dictionary<string, object>> packages = manifest.ContainsKey("packages")
            ? Objects(manifest["packages"], 30) : new List<Dictionary<string, object>> { manifest };
        Dictionary<string, object> full = null;
        foreach (Dictionary<string, object> package in packages)
        {
            string type = Value(package, "package_type") as string ?? "full";
            if (type != "full") continue;
            if (full != null) throw new InvalidDataException("The release contains more than one full installer.");
            full = package;
        }
        if (full == null) throw new InvalidOperationException("The release has only a small update. A full installer must be published for new laptops.");
        Dictionary<string, object> installer = Value(full, "installer") as Dictionary<string, object>;
        if (installer == null) throw new InvalidDataException("The release has no full installer details.");
        string filename = Filename(Value(installer, "filename") ?? Value(installer, "asset_name"));
        if (!filename.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("The release is not a Windows installer.");
        string assetName = Filename(Value(installer, "asset_name") ?? filename);
        string hash = Value(installer, "sha256") as string;
        if (hash == null || !Regex.IsMatch(hash, @"\A[0-9a-fA-F]{64}\z")) throw new InvalidDataException("The installer checksum is missing or invalid.");
        long size = Number(Value(installer, "size"));
        if (size <= 0 || size > MaxBytes) throw new InvalidDataException("The installer size is invalid or exceeds 512 MB.");
        Dictionary<string, object> asset = Asset(assets, assetName);
        if (Number(Value(asset, "size")) != size) throw new InvalidDataException("The installer size does not match the release manifest.");
        return new Release { Version = version, Filename = filename, Sha256 = hash.ToLowerInvariant(), Size = size,
            Url = ApiRoot + "/releases/assets/" + Number(Value(asset, "id")).ToString(System.Globalization.CultureInfo.InvariantCulture) };
    }

    private static Release Latest(CancellationToken cancel)
    {
        Dictionary<string, object> latest = ReadJson(ApiRoot + "/releases/latest", 2 * 1024 * 1024, false, cancel);
        if (!Object.Equals(Value(latest, "draft"), false) || !Object.Equals(Value(latest, "prerelease"), false))
            throw new InvalidOperationException(NoReleaseMessage);
        List<Dictionary<string, object>> assets = Objects(Value(latest, "assets"), 1000);
        Dictionary<string, object> metadata = Asset(assets, "update-manifest.json");
        string address = ApiRoot + "/releases/assets/" + Number(Value(metadata, "id")).ToString(System.Globalization.CultureInfo.InvariantCulture);
        return SelectFull(ReadJson(address, 65536, true, cancel), assets);
    }

    internal static void Verify(string path, Release release)
    {
        if (release.Size <= 0 || release.Size > MaxBytes || !File.Exists(path) || new FileInfo(path).Length != release.Size)
            throw new InvalidDataException("The downloaded installer has the wrong size.");
        using (FileStream stream = File.OpenRead(path))
        {
            if (stream.ReadByte() != 'M' || stream.ReadByte() != 'Z') throw new InvalidDataException("The downloaded file is not a Windows installer.");
            stream.Position = 0;
            using (SHA256 hash = SHA256.Create())
                if (BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant() != release.Sha256)
                    throw new InvalidDataException("The downloaded installer failed its checksum check. It will not be opened.");
        }
    }

    private static string Download(Release release, Action<string, int> progress, CancellationToken cancel)
    {
        string folder = Path.Combine(Path.GetTempPath(), "Leanwatts-Web-Setup-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(folder);
        string path = Path.Combine(folder, release.Filename), partial = path + ".download";
        bool verified = false;
        try
        {
            using (HttpWebResponse response = Open(release.Url, true, cancel))
            using (Stream input = response.GetResponseStream())
            using (FileStream output = new FileStream(partial, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                if (response.ContentLength >= 0 && response.ContentLength != release.Size)
                    throw new InvalidDataException("The download size differs from the release manifest.");
                byte[] buffer = new byte[65536];
                long total = 0;
                int count, lastPercent = -1;
                Stopwatch elapsed = Stopwatch.StartNew();
                while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
                {
                    cancel.ThrowIfCancellationRequested();
                    if (elapsed.Elapsed > TimeSpan.FromMinutes(10)) throw new IOException("The download timed out. Try again or use the offline Setup EXE.");
                    total += count;
                    if (total > release.Size || total > MaxBytes) throw new InvalidDataException("The download exceeds the allowed size.");
                    output.Write(buffer, 0, count);
                    int percent = (int)(total * 100 / release.Size);
                    if (percent != lastPercent)
                    { lastPercent = percent; progress("Downloading version " + release.Version + " · " + percent + "%", percent); }
                }
            }
            cancel.ThrowIfCancellationRequested();
            progress("Verifying the installer…", 100);
            Verify(partial, release);
            File.Move(partial, path);
            verified = true;
            return path;
        }
        finally
        {
            if (!verified)
            {
                // Remove only our known partial file; never recursively delete.
                try { if (File.Exists(partial)) File.Delete(partial); Directory.Delete(folder, false); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }

    private sealed class SetupForm : Form
    {
        private readonly Button start = new Button(), cancel = new Button();
        private readonly Label status = new Label();
        private readonly ProgressBar progress = new ProgressBar();
        private CancellationTokenSource cancellation;
        private bool busy, closeRequested;

        internal SetupForm()
        {
            Text = "Leanwatts Testbench Gen2 · Install latest";
            ClientSize = new Size(650, 340); FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10);
            Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            using (Stream brand = Assembly.GetExecutingAssembly().GetManifestResourceStream("branding.png"))
                if (brand != null) using (Image original = Image.FromStream(brand))
                    Controls.Add(new PictureBox { Image = new Bitmap(original), Left = 24, Top = 14,
                        Width = 140, Height = 48, SizeMode = PictureBoxSizeMode.Zoom });
            Controls.Add(new Label { Text = "Install latest release", Left = 184, Top = 20,
                Width = 445, Height = 38, Font = new Font("Segoe UI", 19, FontStyle.Bold) });
            Controls.Add(new Label { Text = "Download the latest full installer directly from the Leanwatts release channel.",
                Left = 24, Top = 80, Width = 600, Height = 42 });
            Controls.Add(new Label { Text = "Internet required · No GitHub account needed · Saved station data is retained by Setup",
                Left = 24, Top = 124, Width = 600, Height = 42, ForeColor = Color.DimGray });
            status.Text = "Ready. If no release is published yet, use the offline Setup EXE supplied with version 1.1.";
            status.SetBounds(24, 173, 600, 63); Controls.Add(status);
            progress.SetBounds(24, 243, 600, 12); Controls.Add(progress);
            start.Text = "Download and install latest"; start.SetBounds(24, 278, 420, 40);
            cancel.Text = "Close"; cancel.SetBounds(460, 278, 164, 40);
            Controls.Add(start); Controls.Add(cancel);
            cancel.Click += delegate { if (busy) { cancellation.Cancel(); status.Text = "Cancelling download…"; } else Close(); };
            FormClosing += delegate(object sender, FormClosingEventArgs e) {
                if (busy) { closeRequested = true; cancellation.Cancel(); status.Text = "Cancelling download…"; e.Cancel = true; }
            };
            start.Click += async delegate {
                if (busy) return;
                busy = true; start.Enabled = false; cancel.Text = "Cancel";
                cancellation = new CancellationTokenSource();
                status.Text = "Checking the latest published release…"; progress.Value = 0;
                try
                {
                    Release release = await Task.Run(() => Latest(cancellation.Token));
                    string path = await Task.Run(() => Download(release,
                        (text, value) => BeginInvoke((Action)(() => { status.Text = text; progress.Value = value; })), cancellation.Token));
                    cancellation.Token.ThrowIfCancellationRequested();
                    Verify(path, release);
                    Process.Start(new ProcessStartInfo(path) { WorkingDirectory = Path.GetDirectoryName(path), UseShellExecute = false });
                    status.Text = "Verified version " + release.Version + ". The installer is open; follow its steps to finish installation.";
                }
                catch (OperationCanceledException) { status.Text = "Download cancelled. You can retry or use the offline Setup EXE."; }
                catch (Exception ex) { status.Text = "Could not finish: " + ex.Message; }
                finally
                {
                    busy = false; start.Enabled = true; cancel.Text = "Close";
                    cancellation.Dispose(); cancellation = null;
                    if (closeRequested) Close();
                }
            };
        }
    }
}
