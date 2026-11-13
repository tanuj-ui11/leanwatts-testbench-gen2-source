using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace ChargerCalibration
{
    public sealed class Recipe
    {
        public string Name { get; set; }
        public double CcCurrent { get; set; }
        public double EndVoltage { get; set; }
        public double EndCurrent { get; set; }
        public double CurrentTolerance { get; set; }
        public double VoltageTolerance { get; set; }
        public double EndCurrentTolerance { get; set; }
        public double CurrentCalLoadVoltage { get; set; }
        public double VoltageCalLoadCurrent { get; set; }
        public double MaxVoltage { get; set; }
        public double MaxCurrent { get; set; }
        public double MaxPower { get; set; }
        public Dictionary<string, double> DeviceSettings { get; set; }

        private static readonly Dictionary<string, double> settingScales = new Dictionary<string, double>
        {
            { "batteryType", 1 }, { "ratedVoltage", 10 }, { "startVoltage", 10 },
            { "temperatureLimit", 1 }, { "maxChargeMinutes", 1 },
            { "derating110", 1 }, { "derating220", 1 }, { "blindCharge", 1 },
            { "rechargeVoltage", 10 }, { "adjustCurrentMin", 10 },
            { "adjustCurrentMax", 10 }, { "adjustVoltageMin", 10 }, { "adjustVoltageMax", 10 }
        };

        public Recipe()
        {
            Name = "New variant";
            CurrentCalLoadVoltage = 48;
            VoltageCalLoadCurrent = 1;
            DeviceSettings = new Dictionary<string, double>();
        }

        internal static bool Finite(double value)
        {
            return !Double.IsNaN(value) && !Double.IsInfinity(value);
        }

        public string Validate()
        {
            if (String.IsNullOrWhiteSpace(Name)) return "Enter a variant name.";
            double[] values = { CcCurrent, EndVoltage, EndCurrent, CurrentTolerance,
                VoltageTolerance, EndCurrentTolerance, CurrentCalLoadVoltage,
                VoltageCalLoadCurrent, MaxVoltage, MaxCurrent, MaxPower };
            foreach (double value in values)
                if (!Finite(value) || value <= 0) return "All targets, tolerances and limits must be finite numbers greater than zero.";
            if (EndCurrent >= CcCurrent) return "End current must be lower than CC current.";
            if (CurrentCalLoadVoltage >= EndVoltage)
                return "The current-calibration load voltage must be below the charger end voltage; 48 V is not suitable for every variant.";
            if (VoltageCalLoadCurrent >= CcCurrent)
                return "The voltage-calibration load current must be below CC current.";
            if (CurrentTolerance >= CcCurrent || VoltageTolerance >= EndVoltage || EndCurrentTolerance >= EndCurrent)
                return "Each tolerance must be smaller than its target.";
            if (EndVoltage + VoltageTolerance > MaxVoltage)
                return "The end-voltage target and tolerance exceed the voltage limit.";
            if (CcCurrent + CurrentTolerance > MaxCurrent)
                return "The CC-current target and tolerance exceed the current limit.";
            if ((EndVoltage + VoltageTolerance) * (CcCurrent + CurrentTolerance) > MaxPower)
                return "The possible charger output exceeds the entered power limit.";
            if (DeviceSettings != null)
            {
                foreach (KeyValuePair<string, double> setting in DeviceSettings)
                {
                    double scale;
                    if (!settingScales.TryGetValue(setting.Key, out scale))
                        return "Unknown device setting: " + setting.Key + ".";
                    double encoded = setting.Value * scale;
                    double max = setting.Key == "blindCharge" ? 255 : 32767;
                    if (!Finite(setting.Value) || setting.Value < 0 || encoded > max)
                        return "Device setting " + setting.Key + " must be between 0 and " + (max / scale).ToString(CultureInfo.InvariantCulture) + ".";
                    if (Math.Abs(encoded - Math.Round(encoded)) > 0.0000001)
                        return "Device setting " + setting.Key + " must use increments of " + (1 / scale).ToString(CultureInfo.InvariantCulture) + ".";
                }
                if (DeviceSettings.ContainsKey("adjustCurrentMin") && DeviceSettings.ContainsKey("adjustCurrentMax") &&
                    DeviceSettings["adjustCurrentMin"] > DeviceSettings["adjustCurrentMax"])
                    return "Adjustable-current minimum must not exceed its maximum.";
                if (DeviceSettings.ContainsKey("adjustVoltageMin") && DeviceSettings.ContainsKey("adjustVoltageMax") &&
                    DeviceSettings["adjustVoltageMin"] > DeviceSettings["adjustVoltageMax"])
                    return "Adjustable-voltage minimum must not exceed its maximum.";
            }
            return "";
        }

        public Recipe Copy()
        {
            Recipe copy = (Recipe)MemberwiseClone();
            copy.DeviceSettings = DeviceSettings == null ? new Dictionary<string, double>() : new Dictionary<string, double>(DeviceSettings);
            return copy;
        }
    }

    public sealed class Reading
    {
        public double Voltage { get; internal set; }
        public double Current { get; internal set; }
        public double Power { get; internal set; }
        public string LoadMode { get; internal set; }
        public double LoadSetpoint { get; internal set; }
        public bool ChargerOn { get; internal set; }
        public bool LoadOn { get; internal set; }
    }

    public sealed class CheckResult
    {
        public string Name { get; internal set; }
        public double Target { get; internal set; }
        public double Measured { get; internal set; }
        public double Tolerance { get; internal set; }
        public string Unit { get; internal set; }
        public string Status { get; internal set; }
    }

    // This model deliberately contains no hardware I/O. Its output is never a hardware certificate.
    public sealed class CalibrationEngine
    {
        private enum Stage { Idle, Connect, CanStart, LoadCv, Shutdown, Send,
            Readback, StartCurrent, Current, SaveCurrent, LoadOff, LoadCc,
            StartVoltage, Voltage, SaveVoltage, CheckCc, CheckVoltage, CheckEnd, Done, Fault, Aborted }

        private Stage stage;
        private int ticksInStage, totalTicks, stableTicks;
        private bool automatic;
        private DateTime lastTick, runStarted;
        private double previousMeasurement;
        private readonly Recipe recipe;
        private readonly List<CheckResult> checkResults = new List<CheckResult>();

        public Recipe Recipe { get { return recipe.Copy(); } }
        public Reading Reading { get; private set; }
        public string State { get; private set; }
        public string PhaseDetail { get; private set; }
        public string Mode { get; private set; }
        public bool IsRunning { get; private set; }
        public bool CurrentSaved { get; private set; }
        public bool VoltageSaved { get; private set; }
        public double CurrentTrim { get; private set; }
        public double VoltageTrim { get; private set; }
        public List<string> Log { get; private set; }
        public IList<CheckResult> Checks { get { return checkResults.AsReadOnly(); } }
        public bool CanSaveCurrent { get { return stage == Stage.Current && stableTicks >= 4 && WithinCurrent(); } }
        public bool CanSaveVoltage { get { return stage == Stage.Voltage && stableTicks >= 4 && WithinVoltage(); } }
        public string RunId { get; private set; }

        public CalibrationEngine(Recipe recipe)
        {
            if (recipe == null) throw new ArgumentNullException("recipe");
            this.recipe = recipe.Copy();
            string error = this.recipe.Validate();
            if (error.Length != 0) throw new ArgumentException(error, "recipe");
            Reading = new Reading { LoadMode = "OFF" };
            Log = new List<string>();
            State = "Ready";
            Mode = "Simulation";
            RunId = "NOT STARTED";
            PhaseDetail = "Simulation only. No CAN or load-bank connection is made.";
            stage = Stage.Idle;
            AddLog("Simulation model created. No physical charger or load bank is controlled.");
        }

        private void AddLog(string message)
        {
            Log.Add(DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + "  " + message);
        }

        private void SetStage(Stage next, string title, string detail)
        {
            stage = next;
            ticksInStage = 0;
            stableTicks = 0;
            previousMeasurement = Double.NaN;
            State = title;
            PhaseDetail = detail;
            AddLog(title + ": " + detail);
        }

        private void Begin(bool auto, bool voltageOnly, bool crossCheck)
        {
            if (IsRunning) throw new InvalidOperationException("Stop or finish the active operation first.");
            string error = recipe.Validate();
            if (error.Length != 0) throw new InvalidOperationException(error);
            automatic = auto;
            totalTicks = 0;
            lastTick = runStarted = DateTime.UtcNow;
            // Current calibration starts a complete session. Voltage and cross-check
            // continue that session so the exported audit trail has one identity.
            if (!voltageOnly && !crossCheck)
            {
                RunId = DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture);
                Log.Clear();
                AddLog("SIMULATION ONLY. No physical charger or load bank is controlled.");
            }
            checkResults.Clear();
            SafeOff();
            IsRunning = true;
            Mode = crossCheck ? "Simulated cross-check" : auto ? "Automatic simulation" : "Manual simulation";
            AddLog("Begin " + Mode + "; variant " + recipe.Name + ".");
        }

        public void StartAuto()
        {
            Begin(true, false, false);
            CurrentSaved = VoltageSaved = false;
            CurrentTrim = VoltageTrim = 0;
            SetStage(Stage.Connect, "Open device", "Simulate opening the USB-to-CAN adapter.");
        }

        public void StartManualCurrent()
        {
            Begin(false, false, false);
            CurrentSaved = VoltageSaved = false;
            CurrentTrim = VoltageTrim = 0;
            SetStage(Stage.Connect, "Open device", "Simulate opening the USB-to-CAN adapter.");
        }

        public void StartManualVoltage()
        {
            if (!CurrentSaved) throw new InvalidOperationException("Complete and save current calibration first.");
            Begin(false, true, false);
            VoltageSaved = false;
            VoltageTrim = 0;
            SetStage(Stage.LoadOff, "Switch load mode", "Charger and load are off before changing to CC mode.");
        }

        public void AdjustCurrent(double delta)
        {
            if (automatic || stage != Stage.Current)
                throw new InvalidOperationException("Current trim is available only during manual current calibration.");
            if (!Recipe.Finite(delta)) throw new ArgumentException("Enter a finite adjustment.");
            double candidate = CurrentTrim + delta;
            if (Math.Abs(candidate) > Math.Max(1.0, recipe.CcCurrent * 0.15))
                throw new InvalidOperationException("The simulated current trim limit has been reached.");
            CurrentTrim = candidate;
            stableTicks = 0;
            AddLog("Current trim changed to " + Number(CurrentTrim) + " A (simulated engineering-unit correction).");
        }

        public void AdjustVoltage(double delta)
        {
            if (automatic || stage != Stage.Voltage)
                throw new InvalidOperationException("Voltage trim is available only during manual voltage calibration.");
            if (!Recipe.Finite(delta)) throw new ArgumentException("Enter a finite adjustment.");
            double candidate = VoltageTrim + delta;
            if (Math.Abs(candidate) > Math.Max(2.0, recipe.EndVoltage * 0.15))
                throw new InvalidOperationException("The simulated voltage trim limit has been reached.");
            VoltageTrim = candidate;
            stableTicks = 0;
            AddLog("Voltage trim changed to " + Number(VoltageTrim) + " V (simulated engineering-unit correction).");
        }

        public void SaveCurrent()
        {
            if (!CanSaveCurrent) throw new InvalidOperationException("Current must remain stable and within tolerance for four samples before saving.");
            CurrentSaved = true;
            SetStage(Stage.SaveCurrent, "Save full-current calibration", "Simulated save only; no charger memory is written.");
        }

        public void SaveVoltage()
        {
            if (!CanSaveVoltage) throw new InvalidOperationException("Voltage must remain stable and within tolerance for four samples before saving.");
            VoltageSaved = true;
            SetStage(Stage.SaveVoltage, "Save voltage calibration", "Simulated save only; no charger memory is written.");
        }

        public void StartCrossCheck()
        {
            if (!CurrentSaved || !VoltageSaved)
                throw new InvalidOperationException("Save both current and voltage calibrations before cross-checking.");
            Begin(false, false, true);
            BeginCheckCc();
        }

        public void Abort()
        {
            SafeOff();
            IsRunning = false;
            SetStage(Stage.Aborted, "Stopped", "Simulated charger output and load input are off.");
        }

        private void SafeOff()
        {
            Reading.ChargerOn = Reading.LoadOn = false;
            Reading.Voltage = Reading.Current = Reading.Power = 0;
            Reading.LoadMode = "OFF";
            Reading.LoadSetpoint = 0;
        }

        private void Fail(string error)
        {
            SafeOff();
            IsRunning = false;
            SetStage(Stage.Fault, "Fault - stopped", error + " Simulated outputs are off.");
        }

        private bool WithinCurrent() { return Math.Abs(Reading.Current - recipe.CcCurrent) <= recipe.CurrentTolerance; }
        private bool WithinVoltage() { return Math.Abs(Reading.Voltage - recipe.EndVoltage) <= recipe.VoltageTolerance; }

        private void UpdateReading()
        {
            if (!Reading.ChargerOn || !Reading.LoadOn)
            {
                Reading.Voltage = Reading.Current = Reading.Power = 0;
                return;
            }
            double targetVoltage, targetCurrent;
            if (stage == Stage.CheckEnd)
            {
                // An illustrative model, not a validated end-of-charge/termination test.
                targetVoltage = recipe.EndVoltage;
                targetCurrent = recipe.EndCurrent;
            }
            else if (Reading.LoadMode == "CV")
            {
                targetVoltage = Reading.LoadSetpoint;
                targetCurrent = Math.Max(0, recipe.CcCurrent * 0.96 + CurrentTrim);
            }
            else
            {
                targetVoltage = Math.Max(0, recipe.EndVoltage * 0.97 + VoltageTrim);
                targetCurrent = Reading.LoadSetpoint;
            }
            Reading.Voltage += (targetVoltage - Reading.Voltage) * 0.75;
            Reading.Current += (targetCurrent - Reading.Current) * 0.75;
            Reading.Power = Reading.Voltage * Reading.Current;
        }

        private bool EnforceLimits()
        {
            if (!Recipe.Finite(Reading.Voltage) || !Recipe.Finite(Reading.Current) || !Recipe.Finite(Reading.Power))
            { Fail("Non-finite simulated telemetry."); return false; }
            if (Reading.Voltage < 0 || Reading.Current < 0 || Reading.Power < 0 ||
                Reading.Voltage > recipe.MaxVoltage || Reading.Current > recipe.MaxCurrent || Reading.Power > recipe.MaxPower)
            { Fail("A configured voltage, current or power limit was exceeded."); return false; }
            return true;
        }

        private void TrackStability(double measurement, double target, double tolerance)
        {
            if (Math.Abs(measurement - target) <= tolerance && Recipe.Finite(previousMeasurement) &&
                Math.Abs(measurement - previousMeasurement) <= tolerance * 0.20)
                stableTicks++;
            else stableTicks = 0;
            previousMeasurement = measurement;
        }

        public void Tick()
        {
            if (!IsRunning) return;
            DateTime now = DateTime.UtcNow;
            if ((now - lastTick).TotalSeconds > 3)
            { Fail("The simulation update loop stalled for more than three seconds."); return; }
            lastTick = now;
            ticksInStage++;
            totalTicks++;
            if ((automatic && totalTicks > 240) || (!automatic && totalTicks > 3600) || (now - runStarted).TotalMinutes > 15)
            { Fail("Calibration timed out before completion."); return; }

            // Check both before and after the model update so invalid samples cannot be hidden by the model.
            if (!EnforceLimits()) return;
            UpdateReading();
            if (!EnforceLimits()) return;

            switch (stage)
            {
                case Stage.Connect:
                    if (ticksInStage >= 2) SetStage(Stage.CanStart, "Start CAN", "Simulated CAN connection ready.");
                    break;
                case Stage.CanStart:
                    if (ticksInStage >= 2)
                    {
                        Reading.LoadMode = "CV"; Reading.LoadSetpoint = recipe.CurrentCalLoadVoltage; Reading.LoadOn = true;
                        SetStage(Stage.LoadCv, "Load bank ON - CV", "Hold " + Number(recipe.CurrentCalLoadVoltage) + " V for current calibration.");
                    }
                    break;
                case Stage.LoadCv:
                    if (ticksInStage >= 2)
                    {
                        Reading.ChargerOn = false;
                        SetStage(Stage.Shutdown, "Shut down charger", "Simulated stop before writing the variant settings.");
                    }
                    break;
                case Stage.Shutdown:
                    if (ticksInStage >= 2) SetStage(Stage.Send, "Send variant settings", "Simulate sending the saved profile to the charger.");
                    break;
                case Stage.Send:
                    if (ticksInStage >= 2) SetStage(Stage.Readback, "Verify settings readback", "Simulated readback matches every entered setting.");
                    break;
                case Stage.Readback:
                    if (ticksInStage >= 3)
                    {
                        Reading.ChargerOn = true;
                        SetStage(Stage.StartCurrent, "Start current calibration", "Wait for simulated output to settle.");
                    }
                    break;
                case Stage.StartCurrent:
                    if (ticksInStage >= 3) SetStage(Stage.Current, automatic ? "Auto current trim" : "Manual current",
                        "Keep the load in CV. Adjust the charger current trim until the independent load reading matches the target.");
                    break;
                case Stage.Current:
                    TrackStability(Reading.Current, recipe.CcCurrent, recipe.CurrentTolerance);
                    if (automatic && CanSaveCurrent) SaveCurrent();
                    else if (automatic && !WithinCurrent())
                    {
                        CurrentTrim += (recipe.CcCurrent - Reading.Current) * 0.55;
                        if (Math.Abs(CurrentTrim) > Math.Max(1.0, recipe.CcCurrent * 0.15)) Fail("Automatic current trim exceeded its bound.");
                    }
                    break;
                case Stage.SaveCurrent:
                    if (ticksInStage >= 2)
                    {
                        SafeOff();
                        if (automatic) SetStage(Stage.LoadOff, "Switch load mode", "Charger and load are off before changing to CC mode.");
                        else Finish("Current saved (simulation)", "Continue with manual voltage calibration.");
                    }
                    break;
                case Stage.LoadOff:
                    if (ticksInStage >= 2)
                    {
                        Reading.LoadMode = "CC"; Reading.LoadSetpoint = recipe.VoltageCalLoadCurrent; Reading.LoadOn = true;
                        SetStage(Stage.LoadCc, "Load bank ON - CC", "Hold " + Number(recipe.VoltageCalLoadCurrent) + " A for voltage calibration.");
                    }
                    break;
                case Stage.LoadCc:
                    if (ticksInStage >= 2)
                    {
                        Reading.ChargerOn = true;
                        SetStage(Stage.StartVoltage, "Start voltage calibration", "Wait for simulated output to settle.");
                    }
                    break;
                case Stage.StartVoltage:
                    if (ticksInStage >= 3) SetStage(Stage.Voltage, automatic ? "Auto voltage trim" : "Manual voltage",
                        "Keep the load in CC. Adjust the charger voltage trim until the independent load reading matches the target.");
                    break;
                case Stage.Voltage:
                    TrackStability(Reading.Voltage, recipe.EndVoltage, recipe.VoltageTolerance);
                    if (automatic && CanSaveVoltage) SaveVoltage();
                    else if (automatic && !WithinVoltage())
                    {
                        VoltageTrim += (recipe.EndVoltage - Reading.Voltage) * 0.55;
                        if (Math.Abs(VoltageTrim) > Math.Max(2.0, recipe.EndVoltage * 0.15)) Fail("Automatic voltage trim exceeded its bound.");
                    }
                    break;
                case Stage.SaveVoltage:
                    if (ticksInStage >= 2)
                    {
                        SafeOff();
                        if (automatic) BeginCheckCc();
                        else Finish("Voltage saved (simulation)", "Both calibrations are saved in the model. Run the simulated cross-check.");
                    }
                    break;
                case Stage.CheckCc:
                    if (ticksInStage >= 8)
                    {
                        if (!AddCheck("CC current", recipe.CcCurrent, Reading.Current, recipe.CurrentTolerance, "A")) break;
                        SafeOff();
                        Reading.LoadMode = "CC"; Reading.LoadSetpoint = recipe.VoltageCalLoadCurrent;
                        Reading.LoadOn = Reading.ChargerOn = true;
                        SetStage(Stage.CheckVoltage, "Cross-check end voltage", "Simulated voltage measurement under the profile's low-current load.");
                    }
                    break;
                case Stage.CheckVoltage:
                    if (ticksInStage >= 8)
                    {
                        if (!AddCheck("End voltage", recipe.EndVoltage, Reading.Voltage, recipe.VoltageTolerance, "V")) break;
                        SafeOff();
                        Reading.LoadMode = "MODEL"; Reading.LoadSetpoint = recipe.EndCurrent;
                        Reading.LoadOn = Reading.ChargerOn = true;
                        SetStage(Stage.CheckEnd, "Cross-check end current (model)", "Illustrative model only. Actual charge termination and operation without CAN remain unverified.");
                    }
                    break;
                case Stage.CheckEnd:
                    if (ticksInStage >= 8)
                    {
                        if (!AddCheck("End current (illustrative model)", recipe.EndCurrent, Reading.Current, recipe.EndCurrentTolerance, "A")) break;
                        Finish("Simulation complete", "Three simulated checks recorded. No physical calibration or charge-termination test was performed.");
                    }
                    break;
            }
        }

        private void BeginCheckCc()
        {
            if (!CurrentSaved || !VoltageSaved) { Fail("Both calibration saves are required for cross-checking."); return; }
            SafeOff();
            Reading.LoadMode = "CV"; Reading.LoadSetpoint = recipe.CurrentCalLoadVoltage;
            Reading.LoadOn = Reading.ChargerOn = true;
            SetStage(Stage.CheckCc, "Cross-check CC current", "Simulated current measurement at the profile's CV load setting.");
        }

        private bool AddCheck(string name, double target, double measured, double tolerance, string unit)
        {
            bool inTolerance = Recipe.Finite(measured) && Math.Abs(measured - target) <= tolerance;
            checkResults.Add(new CheckResult { Name = name, Target = target, Measured = measured,
                Tolerance = tolerance, Unit = unit, Status = inTolerance ? "SIMULATED IN TOLERANCE" : "SIMULATED OUT OF TOLERANCE" });
            AddLog(name + ": " + Number(measured) + " " + unit + "; target " + Number(target) + " +/- " + Number(tolerance) + ". SIMULATED ONLY.");
            if (!inTolerance) { Fail(name + " was outside tolerance in the simulation."); return false; }
            return true;
        }

        private void Finish(string title, string detail)
        {
            SafeOff();
            IsRunning = false;
            SetStage(Stage.Done, title, detail);
        }

        private static string Number(double value) { return value.ToString("0.######", CultureInfo.InvariantCulture); }
        private static string Csv(string value) { return "\"" + (value ?? "").Replace("\"", "\"\"") + "\""; }
        private static void Row(StringBuilder data, string category, string item, string value, string unit, string status)
        {
            data.AppendLine(Csv(category) + "," + Csv(item) + "," + Csv(value) + "," + Csv(unit) + "," + Csv(status));
        }

        public void ExportReport(string path)
        {
            if (String.IsNullOrWhiteSpace(path)) throw new ArgumentException("Choose a report file.");
            StringBuilder data = new StringBuilder();
            data.AppendLine("Category,Item,Value,Unit,Status");
            Row(data, "WARNING", "Validity", "SIMULATION ONLY - NOT A HARDWARE CALIBRATION CERTIFICATE", "", "NO HARDWARE TEST");
            Row(data, "Run", "ID", RunId, "", State);
            Row(data, "Run", "Exported UTC", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture), "", Mode);
            Row(data, "Profile", "Variant", recipe.Name, "", "SIMULATED");
            Row(data, "Profile", "CC current", Number(recipe.CcCurrent), "A", "Target");
            Row(data, "Profile", "End voltage", Number(recipe.EndVoltage), "V", "Target");
            Row(data, "Profile", "End current", Number(recipe.EndCurrent), "A", "Target");
            Row(data, "Profile", "Current tolerance", Number(recipe.CurrentTolerance), "A", "Absolute tolerance");
            Row(data, "Profile", "Voltage tolerance", Number(recipe.VoltageTolerance), "V", "Absolute tolerance");
            Row(data, "Profile", "End-current tolerance", Number(recipe.EndCurrentTolerance), "A", "Absolute tolerance");
            Row(data, "Profile", "Current-calibration CV load", Number(recipe.CurrentCalLoadVoltage), "V", "Load setting");
            Row(data, "Profile", "Voltage-calibration CC load", Number(recipe.VoltageCalLoadCurrent), "A", "Load setting");
            Row(data, "Profile", "Voltage limit", Number(recipe.MaxVoltage), "V", "Limit");
            Row(data, "Profile", "Current limit", Number(recipe.MaxCurrent), "A", "Limit");
            Row(data, "Profile", "Power limit", Number(recipe.MaxPower), "W", "Limit");
            foreach (KeyValuePair<string, double> setting in recipe.DeviceSettings)
                Row(data, "Device setting", setting.Key, Number(setting.Value), "", "Stored only; engineering validation required");
            Row(data, "Calibration", "Current saved", CurrentSaved.ToString(), "", "SIMULATED ONLY");
            Row(data, "Calibration", "Voltage saved", VoltageSaved.ToString(), "", "SIMULATED ONLY");
            Row(data, "Calibration", "Current trim", Number(CurrentTrim), "A", "Model correction; not a charger register value");
            Row(data, "Calibration", "Voltage trim", Number(VoltageTrim), "V", "Model correction; not a charger register value");
            foreach (CheckResult check in checkResults)
            {
                Row(data, "Check", check.Name, Number(check.Measured), check.Unit, check.Status);
                Row(data, "Check target", check.Name, Number(check.Target), check.Unit, "+/- " + Number(check.Tolerance));
            }
            Row(data, "Unverified", "End-of-charge termination", "Requires a verified physical test procedure", "", "NOT TESTED");
            Row(data, "Unverified", "Operation without CAN commands", "Variant behavior must be confirmed", "", "NOT TESTED");
            foreach (string entry in Log) Row(data, "Event", "Log", entry, "", "SIMULATION");
            File.WriteAllText(path, data.ToString(), new UTF8Encoding(true));
            AddLog("Simulation report exported to " + Path.GetFileName(path) + ".");
        }
    }
}
