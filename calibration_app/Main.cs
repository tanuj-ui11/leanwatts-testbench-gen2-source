using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.Serialization.Json;
using System.Windows.Forms;

namespace ChargerCalibration
{
    static class Program
    {
        [STAThread] public static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new BenchForm(args));
        }
    }

    public class BenchForm : Form
    {
        readonly Color Navy = Color.FromArgb(17, 34, 52), Teal = Color.FromArgb(0, 112, 115), Muted = Color.FromArgb(88, 106, 123);
        readonly string Home = AppDomain.CurrentDomain.BaseDirectory;
        readonly Dictionary<string, TextBox> Fields = new Dictionary<string, TextBox>();
        readonly List<Recipe> Profiles = new List<Recipe>();
        readonly Timer Clock = new Timer();
        readonly string[] Args;
        CalibrationEngine Engine;
        ComboBox ProfileSelect;
        Label Voltage, Current, Power, StateText, Detail, ProfileSummary, LoadText, SavedText, TrimText;
        TextBox RunEvents;
        TabControl Tabs;
        NumericUpDown TrimStep;
        Button Auto, ManualI, ManualV, Cross, SaveI, SaveV, UpI, DownI, UpV, DownV, SaveProfile;
        int SeenLogs;

        public BenchForm(string[] args)
        {
            Args = args;
            int dataArgument = Array.IndexOf(args, "--data-dir");
            if (dataArgument >= 0 && dataArgument + 1 < args.Length) Home = Path.GetFullPath(args[dataArgument + 1]);
            Text = "Charger Calibration | Engineering Preview";
            Rectangle area = Screen.PrimaryScreen.WorkingArea;
            Size = new Size(Math.Min(1280, area.Width), Math.Min(900, area.Height));
            MinimumSize = new Size(1000, 640);
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Segoe UI", 10);
            BackColor = Color.FromArgb(240, 244, 247);
            AutoScaleMode = AutoScaleMode.Dpi;
            Build();
            LoadProfiles();
            Clock.Interval = 250;
            Clock.Tick += delegate { if (Engine != null) { try { Engine.Tick(); } catch (Exception ex) { Engine.Abort(); Notice(ex.Message); } RefreshBench(); } };
            Clock.Start();
            FormClosing += delegate { if (Engine != null) Engine.Abort(); Clock.Stop(); };
            Shown += delegate {
                if (Array.IndexOf(Args, "--preview") >= 0)
                {
                    using (Bitmap shot = new Bitmap(Width, Height)) { DrawToBitmap(shot, new Rectangle(0, 0, Width, Height)); shot.Save(Path.Combine(Home, "preview.png")); }
                    Close();
                }
            };
        }

        Label LabelAt(Control parent, string text, int x, int y, int w, int h, float size, Color color, bool bold)
        {
            Label label = new Label { Text = text, Location = new Point(x,y), Size = new Size(w,h), ForeColor = color,
                Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular) };
            parent.Controls.Add(label); return label;
        }
        Button ButtonAt(Control parent, string text, int x, int y, int w, EventHandler action, bool primary)
        {
            Button button = new Button { Text = text, Location = new Point(x,y), Size = new Size(w,38), FlatStyle = FlatStyle.Flat,
                BackColor = primary ? Teal : Color.White, ForeColor = primary ? Color.White : Navy, Cursor = Cursors.Hand };
            button.FlatAppearance.BorderColor = primary ? Teal : Color.FromArgb(200,211,220);
            button.Click += action; parent.Controls.Add(button); return button;
        }
        Panel Card(Control parent, int x, int y, int w, int h)
        {
            Panel card = new Panel { Location = new Point(x,y), Size = new Size(w,h), BackColor = Color.White };
            parent.Controls.Add(card); return card;
        }
        void Build()
        {
            Panel header = new Panel { Dock = DockStyle.Top, Height = 94, BackColor = Navy };
            Controls.Add(header);
            LabelAt(header, "CHARGER / CALIBRATION BENCH", 25,18,780,34,22,Color.White,true);
            LabelAt(header, "USBCAN-I (C+)  /  Maynuo M8437B  /  Saved variant profiles", 27,57,870,24,10,Color.FromArgb(176,197,211),false);
            Button stop = ButtonAt(header,"STOP SIMULATION",1000,28,220,delegate { if (Engine != null) Engine.Abort(); RefreshBench(); }, false);
            stop.BackColor = Color.FromArgb(180,46,55); stop.ForeColor = Color.White; stop.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            Panel body = new Panel { Dock = DockStyle.Fill, Padding = new Padding(20,14,20,10) };
            Controls.Add(body); body.BringToFront();
            Tabs = new TabControl { Dock = DockStyle.Fill, Font = new Font("Segoe UI", 10) };
            body.Controls.Add(Tabs);
            TabPage bench = new TabPage("  Calibration bench  ") { BackColor = BackColor, AutoScroll = true };
            TabPage variants = new TabPage("  Variant profiles  ") { BackColor = BackColor, AutoScroll = true };
            TabPage settings = new TabPage("  Charger parameters  ") { BackColor = BackColor, AutoScroll = true };
            TabPage readiness = new TabPage("  Hardware readiness  ") { BackColor = BackColor, AutoScroll = true };
            Tabs.TabPages.AddRange(new TabPage[] { bench, variants, settings, readiness });
            BuildBench(bench); BuildProfiles(variants); BuildDeviceSettings(settings); BuildReadiness(readiness);
        }

        void BuildBench(TabPage page)
        {
            LabelAt(page, "SIMULATION ONLY  —  No devices connected or hardware commands sent", 16,14,1150,26,11,Color.FromArgb(151,87,0),true);
            Panel setup = Card(page,16,51,1160,94);
            LabelAt(setup,"VARIANT",16,12,130,20,9,Muted,true);
            ProfileSelect = new ComboBox { Location = new Point(16,37), Size = new Size(320,28), DropDownStyle = ComboBoxStyle.DropDownList };
            setup.Controls.Add(ProfileSelect);
            ProfileSelect.SelectedIndexChanged += delegate { SelectProfile(); };
            ProfileSummary = LabelAt(setup,"",360,22,595,56,11,Navy,false);
            ButtonAt(setup,"Edit profiles",970,30,168,delegate { Tabs.SelectedIndex = 1; },false);
            Panel v = Card(page,16,158,376,106), i = Card(page,408,158,376,106), p = Card(page,800,158,376,106);
            LabelAt(v,"SIMULATED OUTPUT VOLTAGE",16,12,340,22,9,Muted,true);
            LabelAt(i,"SIMULATED OUTPUT CURRENT",16,12,340,22,9,Muted,true);
            LabelAt(p,"SIMULATED OUTPUT POWER",16,12,340,22,9,Muted,true);
            Voltage = LabelAt(v,"0.00 V",16,38,345,60,29,Navy,true);
            Current = LabelAt(i,"0.00 A",16,38,345,60,29,Navy,true);
            Power = LabelAt(p,"0 W",16,38,345,60,29,Navy,true);
            Panel workflow = Card(page,16,278,740,195);
            LabelAt(workflow,"CALIBRATION WORKFLOW",16,12,690,24,10,Muted,true);
            StateText = LabelAt(workflow,"Ready",16,42,690,35,19,Navy,true);
            Detail = LabelAt(workflow,"",16,82,700,52,10,Muted,false);
            Auto = ButtonAt(workflow,"Run automatic",16,145,160,delegate { Act(delegate { Engine.StartAuto(); }); },true);
            ManualI = ButtonAt(workflow,"Manual current",188,145,165,delegate { Act(delegate { Engine.StartManualCurrent(); }); },false);
            ManualV = ButtonAt(workflow,"Manual voltage",365,145,165,delegate { Act(delegate { Engine.StartManualVoltage(); }); },false);
            Cross = ButtonAt(workflow,"Cross-check",542,145,180,delegate { Act(delegate { Engine.StartCrossCheck(); }); },false);
            Panel trim = Card(page,772,278,404,285);
            LabelAt(trim,"MANUAL CHARGER TRIM",16,12,365,24,10,Muted,true);
            LabelAt(trim,"Hold load CV for current; load CC for voltage.\nAdjust charger trim, then save after settling.",16,41,370,47,10,Navy,false);
            LabelAt(trim,"Model correction step (A / V)",16,100,245,24,10,Muted,false);
            TrimStep = new NumericUpDown { Location = new Point(268,97), Width = 112, DecimalPlaces = 2, Minimum = 0.01M, Maximum = 1, Increment = 0.01M, Value = 0.10M };
            trim.Controls.Add(TrimStep);
            DownI = ButtonAt(trim,"I −",16,137,60,delegate { Act(delegate { Engine.AdjustCurrent(-(double)TrimStep.Value); }); },false);
            UpI = ButtonAt(trim,"I +",84,137,60,delegate { Act(delegate { Engine.AdjustCurrent((double)TrimStep.Value); }); },false);
            SaveI = ButtonAt(trim,"Save full current",156,137,224,delegate { Act(delegate { Engine.SaveCurrent(); }); },true);
            DownV = ButtonAt(trim,"V −",16,184,60,delegate { Act(delegate { Engine.AdjustVoltage(-(double)TrimStep.Value); }); },false);
            UpV = ButtonAt(trim,"V +",84,184,60,delegate { Act(delegate { Engine.AdjustVoltage((double)TrimStep.Value); }); },false);
            SaveV = ButtonAt(trim,"Save voltage",156,184,224,delegate { Act(delegate { Engine.SaveVoltage(); }); },true);
            TrimText = LabelAt(trim,"",16,238,370,30,10,Muted,false);
            Panel status = Card(page,16,487,740,76);
            LoadText = LabelAt(status,"Load off",16,12,700,24,10,Navy,false);
            SavedText = LabelAt(status,"Current: pending  /  Voltage: pending",16,41,700,24,10,Muted,false);
            LabelAt(page,"RUN EVENTS",16,579,600,24,10,Muted,true);
            ButtonAt(page,"Export run report",975,574,201,delegate { ExportReport(); },false);
            RunEvents = new TextBox { Location = new Point(16,620), Size = new Size(1160,106), Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical,
                Font = new Font("Consolas",9), BackColor = Color.FromArgb(229,236,241), ForeColor = Navy, BorderStyle = BorderStyle.None };
            page.Controls.Add(RunEvents);
        }

        void BuildProfiles(TabPage page)
        {
            LabelAt(page,"Set up a variant once. Reuse it for every charger.",22,20,1100,38,21,Navy,true);
            LabelAt(page,"Targets, tolerances and bench limits are stored locally. Demo values are illustrative only.",24,66,1120,26,11,Muted,false);
            Panel card = Card(page,22,110,1148,540);
            Field(card,"Name","Variant name",24,22,500);
            Field(card,"CcCurrent","CC current target (A)",24,105,310);
            Field(card,"EndVoltage","End voltage target (V)",392,105,310);
            Field(card,"EndCurrent","End current target (A)",760,105,310);
            Field(card,"CurrentTolerance","Current tolerance (± A)",24,198,310);
            Field(card,"VoltageTolerance","Voltage tolerance (± V)",392,198,310);
            Field(card,"EndCurrentTolerance","End current tolerance (± A)",760,198,310);
            Field(card,"CurrentCalLoadVoltage","Current calibration: load CV (V)",24,291,310);
            Field(card,"VoltageCalLoadCurrent","Voltage calibration: load CC (A)",392,291,310);
            Field(card,"MaxVoltage","Bench voltage limit (V)",760,291,310);
            Field(card,"MaxCurrent","Bench current limit (A)",24,384,310);
            Field(card,"MaxPower","Bench power limit (W)",392,384,310);
            SaveProfile = ButtonAt(card,"Save variant profile",760,417,310,delegate { SaveVariant(); },true);
            LabelAt(card,"Saving a new name creates a separate profile. Existing names update that profile. Profiles cannot change during a run.",24,488,1090,40,10,Muted,false);
        }
        void Field(Control parent, string key, string title, int x, int y, int width)
        {
            LabelAt(parent,title,x,y,width,26,10,Muted,true);
            TextBox box = new TextBox { Location = new Point(x,y+31), Width = width, Font = new Font("Segoe UI",13) };
            parent.Controls.Add(box); Fields[key] = box;
        }
        void BuildDeviceSettings(TabPage page)
        {
            LabelAt(page,"Original charger parameters",22,20,1100,40,22,Navy,true);
            LabelAt(page,"Stored with the selected variant. Blank values stay unspecified; they are not sent to hardware.",24,70,1110,28,11,Muted,false);
            Panel card = Card(page,22,118,1148,566);
            string[,] fields = {
                {"batteryType","Battery type (numeric code)"}, {"ratedVoltage","Rated voltage (V)"}, {"startVoltage","Start / trickle voltage (V)"},
                {"temperatureLimit","Protection temperature (°C)"}, {"maxChargeMinutes","Maximum charge time (min)"}, {"rechargeVoltage","Recharge voltage (V)"},
                {"derating110","110 V derating (%)"}, {"derating220","220 V derating (%)"}, {"blindCharge","Blind-charge mode (numeric code)"},
                {"adjustCurrentMin","Adjustable current minimum (A)"}, {"adjustCurrentMax","Adjustable current maximum (A)"}, {"adjustVoltageMin","Adjustable voltage minimum (V)"},
                {"adjustVoltageMax","Adjustable voltage maximum (V)"}
            };
            for (int n = 0; n < fields.GetLength(0); n++) Field(card,"Device:"+fields[n,0],fields[n,1],24+(n%3)*368,20+(n/3)*92,310);
            LabelAt(card,"CC current, end voltage and end current are set on the Variant profiles tab. Mode-code meanings still need confirmation.\nThese additional fields are optional for simulation. Save the variant profile to retain edits made on either tab.",24,482,1090,66,10,Muted,false);
            ButtonAt(page,"Save variant profile",852,698,318,delegate { SaveVariant(); },true);
        }
        void BuildReadiness(TabPage page)
        {
            LabelAt(page,"Hardware commissioning",22,20,1100,40,22,Navy,true);
            LabelAt(page,"Live operation is unavailable in this engineering preview.",24,70,1100,30,12,Color.FromArgb(151,87,0),true);
            Panel card = Card(page,22,122,1148,500);
            string text = "IDENTIFIED\r\nUSBCAN-I (C+) adapter, shown in your photos. Charger vendor application and driver files are available for static analysis.\r\n\r\nTO VERIFY BEFORE LIVE CONTROL\r\n1. Adapter driver / DLL compatibility, CAN channel, bitrate and charger addressing.\r\n2. Maynuo M8437B laptop connection and its exact remote programming manual.\r\n3. Charger trim/save commands, acknowledgement and persistence after a power cycle.\r\n4. Variant targets and tolerances, load ratings, independent measurement accuracy.\r\n5. Meaning of non-CAN operation and the real end-current / termination test.\r\n\r\nWHAT THIS BUILD DOES\r\nSaves profiles; exercises manual and automatic calibration in a simulated bench; checks operating limits; exports simulation reports. It does not open a CAN device, control the load, or write charger calibration memory.\r\n\r\nCALIBRATION SEQUENCE\r\nCurrent: load CV (typically 48 V) → charger shutdown → send profile → verify readback → start → adjust charger current trim → settle → full-current save.\r\nVoltage: load CC (typically 1 A) → start → adjust charger voltage trim → settle → voltage save.\r\nCross-check: CC current, end voltage, end current; the production test must verify actual charger transitions and persistence.";
            TextBox evidence = new TextBox { Location = new Point(22,18), Size = new Size(1100,464), Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, BorderStyle = BorderStyle.None, BackColor = Color.White, ForeColor = Navy, Font = new Font("Segoe UI",11), Text = text };
            card.Controls.Add(evidence);
        }

        Recipe Demo()
        {
            return new Recipe { Name = "DEMO / 58.4 V · 15 A", CcCurrent = 15, EndVoltage = 58.4, EndCurrent = 0.5,
                CurrentTolerance = 0.1, VoltageTolerance = 0.1, EndCurrentTolerance = 0.05,
                CurrentCalLoadVoltage = 48, VoltageCalLoadCurrent = 1, MaxVoltage = 65, MaxCurrent = 20, MaxPower = 1200 };
        }
        void LoadProfiles()
        {
            string file = Path.Combine(Home,"profiles.json");
            if (File.Exists(file))
            {
                try { using (FileStream stream = File.OpenRead(file)) { List<Recipe> saved = (List<Recipe>)new DataContractJsonSerializer(typeof(List<Recipe>)).ReadObject(stream); foreach (Recipe r in saved) { if (String.IsNullOrEmpty(r.Validate())) Profiles.Add(r); } } }
                catch (Exception ex) { Notice("Saved profiles could not be read. The original file is unchanged. " + ex.Message); }
            }
            if (Profiles.Count == 0) Profiles.Add(Demo());
            UpdateProfileList(0);
        }
        void UpdateProfileList(int index)
        {
            ProfileSelect.Items.Clear(); foreach (Recipe p in Profiles) ProfileSelect.Items.Add(p.Name); ProfileSelect.SelectedIndex = index;
        }
        void SelectProfile()
        {
            if (ProfileSelect.SelectedIndex < 0) return;
            Recipe recipe = Profiles[ProfileSelect.SelectedIndex];
            Engine = new CalibrationEngine(recipe); SeenLogs = 0; RunEvents.Clear();
            foreach (KeyValuePair<string,TextBox> field in Fields)
            {
                if (field.Key.StartsWith("Device:"))
                {
                    double setting;
                    field.Value.Text = recipe.DeviceSettings != null && recipe.DeviceSettings.TryGetValue(field.Key.Substring(7),out setting) ? setting.ToString(CultureInfo.InvariantCulture) : "";
                    continue;
                }
                object value = typeof(Recipe).GetProperty(field.Key).GetValue(recipe,null);
                field.Value.Text = Convert.ToString(value,CultureInfo.InvariantCulture);
            }
            ProfileSummary.Text = String.Format(CultureInfo.InvariantCulture,"CC  {0:0.###} A     /     End  {1:0.###} V     /     Cutoff target  {2:0.###} A\nLoad CV  {3:0.###} V     /     Load CC  {4:0.###} A",recipe.CcCurrent,recipe.EndVoltage,recipe.EndCurrent,recipe.CurrentCalLoadVoltage,recipe.VoltageCalLoadCurrent);
            RefreshBench();
        }
        void SaveVariant()
        {
            try
            {
                if (Engine != null && Engine.IsRunning) throw new InvalidOperationException("Stop the run before editing a variant.");
                Recipe recipe = new Recipe();
                foreach (KeyValuePair<string,TextBox> field in Fields)
                {
                    if (field.Key.StartsWith("Device:"))
                    {
                        if (String.IsNullOrWhiteSpace(field.Value.Text)) continue;
                        double setting;
                        if (!Double.TryParse(field.Value.Text,NumberStyles.Float,CultureInfo.InvariantCulture,out setting)) throw new ArgumentException("Enter a number or leave blank for " + field.Key.Substring(7) + ".");
                        recipe.DeviceSettings[field.Key.Substring(7)] = setting;
                        continue;
                    }
                    var property = typeof(Recipe).GetProperty(field.Key);
                    if (field.Key == "Name") property.SetValue(recipe,field.Value.Text.Trim(),null);
                    else { double value; if (!Double.TryParse(field.Value.Text,NumberStyles.Float,CultureInfo.InvariantCulture,out value)) throw new ArgumentException("Enter a number using a decimal point for " + field.Key + "."); property.SetValue(recipe,value,null); }
                }
                string error = recipe.Validate(); if (!String.IsNullOrEmpty(error)) throw new ArgumentException(error);
                int index = Profiles.FindIndex(delegate(Recipe r) { return String.Equals(r.Name,recipe.Name,StringComparison.OrdinalIgnoreCase); });
                List<Recipe> updated = new List<Recipe>(Profiles); if (index < 0) { updated.Add(recipe); index = updated.Count-1; } else updated[index] = recipe;
                string dest = Path.Combine(Home,"profiles.json"), temp = dest + ".tmp";
                using (FileStream stream = File.Create(temp)) new DataContractJsonSerializer(typeof(List<Recipe>)).WriteObject(stream,updated);
                if (File.Exists(dest)) File.Replace(temp,dest,dest+".bak"); else File.Move(temp,dest);
                Profiles.Clear(); Profiles.AddRange(updated); UpdateProfileList(index); Tabs.SelectedIndex = 0;
            }
            catch (Exception ex) { Notice(ex.Message); }
        }
        void Act(Action action)
        {
            try { action(); } catch (Exception ex) { Notice(ex.Message); } RefreshBench();
        }
        void Notice(string message) { MessageBox.Show(this,message,"Calibration bench",MessageBoxButtons.OK,MessageBoxIcon.Information); }
        void RefreshBench()
        {
            if (Engine == null) return;
            Reading r = Engine.Reading;
            Voltage.Text = r.Voltage.ToString("0.00",CultureInfo.InvariantCulture)+" V";
            Current.Text = r.Current.ToString("0.00",CultureInfo.InvariantCulture)+" A";
            Power.Text = r.Power.ToString("0.0",CultureInfo.InvariantCulture)+" W";
            StateText.Text = Engine.State; Detail.Text = Engine.PhaseDetail;
            LoadText.Text = "Charger " + (r.ChargerOn ? "on" : "off") + "  /  Load " + (r.LoadOn ? "on" : "off") + "  /  " + r.LoadMode + " " + r.LoadSetpoint.ToString("0.###",CultureInfo.InvariantCulture) + "  /  simulated reference";
            SavedText.Text = "Current save: " + (Engine.CurrentSaved ? "simulated" : "pending") + "    /    Voltage save: " + (Engine.VoltageSaved ? "simulated" : "pending");
            TrimText.Text = String.Format(CultureInfo.InvariantCulture,"Model correction   {0:0.00} A   /   {1:0.00} V",Engine.CurrentTrim,Engine.VoltageTrim);
            ProfileSelect.Enabled = SaveProfile.Enabled = !Engine.IsRunning;
            Auto.Enabled = ManualI.Enabled = !Engine.IsRunning;
            ManualV.Enabled = !Engine.IsRunning && Engine.CurrentSaved;
            Cross.Enabled = !Engine.IsRunning && Engine.CurrentSaved && Engine.VoltageSaved;
            bool manualI = Engine.State == "Manual current", manualV = Engine.State == "Manual voltage";
            UpI.Enabled = DownI.Enabled = manualI;
            UpV.Enabled = DownV.Enabled = manualV;
            SaveI.Enabled = manualI && Engine.CanSaveCurrent;
            SaveV.Enabled = manualV && Engine.CanSaveVoltage;
            if (SeenLogs > Engine.Log.Count) { SeenLogs = 0; RunEvents.Clear(); }
            while (SeenLogs < Engine.Log.Count) RunEvents.AppendText(Engine.Log[SeenLogs++] + Environment.NewLine);
        }
        void ExportReport()
        {
            if (Engine == null) return;
            using (SaveFileDialog dialog = new SaveFileDialog { Filter = "Simulation report (*.csv)|*.csv", FileName = "SIMULATION-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".csv", InitialDirectory = Home })
                if (dialog.ShowDialog(this) == DialogResult.OK) Act(delegate { Engine.ExportReport(dialog.FileName); });
        }
    }
}
