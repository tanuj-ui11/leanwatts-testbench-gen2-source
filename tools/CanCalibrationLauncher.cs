using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Leanwatts Testbench Gen2")]
[assembly: AssemblyVersion("1.1.0.0")]
[assembly: AssemblyFileVersion("1.1.0.0")]

internal static class Launcher
{
    [STAThread]
    private static void Main()
    {
        string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string python = Path.Combine(root, "runtime", "pythonw.exe");
        string script = Path.Combine(root, "launch_calibration.py");
        if (!File.Exists(python) || !File.Exists(script))
        { MessageBox.Show("The installation is incomplete. Run Leanwatts Testbench Gen2 Setup again.", "Leanwatts Testbench Gen2"); return; }
        try
        {
            ProcessStartInfo info = new ProcessStartInfo(python, "\"" + script + "\"");
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.EnvironmentVariables["PYQTGRAPH_QT_LIB"] = "PySide6";
            Process.Start(info);
        }
        catch (Exception ex) { MessageBox.Show("Could not open Leanwatts Testbench Gen2: " + ex.Message, "Leanwatts Testbench Gen2"); }
    }
}
