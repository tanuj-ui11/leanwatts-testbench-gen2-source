using System;
using System.IO;
using System.Runtime.Serialization.Json;
using System.Threading;

namespace ChargerCalibration
{
    public static class CoreTests
    {
        private static int passed;

        private static Recipe Profile()
        {
            return new Recipe { Name = "TEST ONLY / 58.4 V 15 A", CcCurrent = 15,
                EndVoltage = 58.4, EndCurrent = 0.5, CurrentTolerance = 0.05,
                VoltageTolerance = 0.10, EndCurrentTolerance = 0.05,
                CurrentCalLoadVoltage = 48, VoltageCalLoadCurrent = 1,
                MaxVoltage = 60, MaxCurrent = 16, MaxPower = 1000 };
        }

        private static void Assert(bool condition, string message)
        {
            if (!condition) throw new Exception(message);
        }

        private static void Throws(Action action, string message)
        {
            bool threw = false;
            try { action(); } catch (InvalidOperationException) { threw = true; }
            catch (ArgumentException) { threw = true; }
            Assert(threw, message);
        }

        private static void Test(string name, Action test)
        {
            test();
            passed++;
            Console.WriteLine("OK " + name);
        }

        private static void Until(CalibrationEngine engine, Func<bool> predicate, int limit)
        {
            for (int i = 0; i < limit && !predicate(); i++) engine.Tick();
            Assert(predicate(), "Expected state was not reached; state = " + engine.State);
        }

        private static void Ticks(CalibrationEngine engine, int count)
        {
            for (int i = 0; i < count; i++) engine.Tick();
        }

        private static void SaveManualCurrent(CalibrationEngine engine)
        {
            engine.StartManualCurrent();
            Until(engine, () => engine.State == "Manual current", 25);
            engine.AdjustCurrent(engine.Recipe.CcCurrent * 0.04);
            Until(engine, () => engine.CanSaveCurrent, 30);
            engine.SaveCurrent();
            Until(engine, () => !engine.IsRunning, 10);
        }

        public static int Main()
        {
            try
            {
                Test("reject non-finite and zero recipe values", () => {
                    Recipe p = Profile(); p.CcCurrent = Double.NaN;
                    Assert(p.Validate().Length > 0, "NaN accepted");
                    p = Profile(); p.MaxPower = Double.PositiveInfinity;
                    Assert(p.Validate().Length > 0, "Infinity accepted");
                    p = Profile(); p.EndCurrentTolerance = 0;
                    Assert(p.Validate().Length > 0, "Zero tolerance accepted");
                    Throws(() => new CalibrationEngine(p), "Invalid profile accepted by engine");
                });
                Test("48 V not silently used for lower-voltage variants", () => {
                    Recipe p = Profile(); p.EndVoltage = 29.6;
                    Assert(p.Validate().Length > 0, "Unsuitable load voltage accepted");
                    p.CurrentCalLoadVoltage = 24;
                    Assert(p.Validate().Length == 0, "Corrected variant rejected");
                });
                Test("recipe targets and tolerances must fit all output limits", () => {
                    Recipe p = Profile(); p.MaxVoltage = 58.45;
                    Assert(p.Validate().Length > 0, "Voltage limit ignored");
                    p = Profile(); p.MaxCurrent = 15.01;
                    Assert(p.Validate().Length > 0, "Current limit ignored");
                    p = Profile(); p.MaxPower = 880;
                    Assert(p.Validate().Length > 0, "Power envelope ignored");
                });
                Test("profile snapshot cannot change during a run", () => {
                    Recipe p = Profile(); CalibrationEngine e = new CalibrationEngine(p);
                    p.CcCurrent = 100; Recipe exposed = e.Recipe; exposed.CcCurrent = 200;
                    Assert(e.Recipe.CcCurrent == 15, "Profile snapshot was mutated");
                });
                Test("optional device fields validate encoded ranges and increments", () => {
                    Recipe p = Profile(); p.DeviceSettings["ratedVoltage"] = 48.1;
                    Assert(p.Validate().Length == 0, "Valid optional field rejected");
                    p.DeviceSettings["ratedVoltage"] = 48.15;
                    Assert(p.Validate().Length > 0, "Unrepresentable precision accepted");
                    p.DeviceSettings["ratedVoltage"] = 3276.8;
                    Assert(p.Validate().Length > 0, "Signed readback overflow accepted");
                    p.DeviceSettings.Clear(); p.DeviceSettings["blindCharge"] = 256;
                    Assert(p.Validate().Length > 0, "One-byte field overflow accepted");
                    p.DeviceSettings.Clear(); p.DeviceSettings["unknown"] = 1;
                    Assert(p.Validate().Length > 0, "Unknown device field accepted");
                    p.DeviceSettings.Clear(); p.DeviceSettings["temperatureLimit"] = Double.NaN;
                    Assert(p.Validate().Length > 0, "Non-finite optional field accepted");
                    p.DeviceSettings.Clear(); p.DeviceSettings["adjustCurrentMin"] = 2; p.DeviceSettings["adjustCurrentMax"] = 1;
                    Assert(p.Validate().Length > 0, "Reversed adjustable range accepted");
                });
                Test("device fields survive JSON persistence and are deeply copied", () => {
                    Recipe p = Profile(); p.DeviceSettings["ratedVoltage"] = 48;
                    Recipe copy = p.Copy(); copy.DeviceSettings["ratedVoltage"] = 72;
                    Assert(p.DeviceSettings["ratedVoltage"] == 48, "Dictionary was shallow copied");
                    CalibrationEngine e = new CalibrationEngine(p);
                    p.DeviceSettings["ratedVoltage"] = 96;
                    Recipe exposed = e.Recipe; exposed.DeviceSettings["ratedVoltage"] = 120;
                    Assert(e.Recipe.DeviceSettings["ratedVoltage"] == 48, "Engine device profile mutated");
                    using (MemoryStream stream = new MemoryStream())
                    {
                        DataContractJsonSerializer serializer = new DataContractJsonSerializer(typeof(Recipe));
                        serializer.WriteObject(stream, copy); stream.Position = 0;
                        Recipe restored = (Recipe)serializer.ReadObject(stream);
                        Assert(restored.DeviceSettings["ratedVoltage"] == 72 && restored.Validate().Length == 0,
                            "Device setting JSON round-trip failed");
                    }
                    p.DeviceSettings = null;
                    Assert(p.Validate().Length == 0 && p.Copy().DeviceSettings.Count == 0, "Legacy profile without optional fields rejected");
                });
                Test("auto completes with simulated checks and outputs off", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    Assert(!e.CurrentSaved && !e.VoltageSaved && e.Checks.Count == 0, "Premature results");
                    Until(e, () => !e.IsRunning, 240);
                    Assert(e.State == "Simulation complete", "Auto fault: " + e.PhaseDetail);
                    Assert(e.CurrentSaved && e.VoltageSaved && e.Checks.Count == 3, "Incomplete auto results");
                    Assert(!e.Reading.ChargerOn && !e.Reading.LoadOn && e.Reading.Power == 0, "Outputs left on");
                    foreach (CheckResult c in e.Checks) Assert(c.Status == "SIMULATED IN TOLERANCE", "Incorrect result status");
                });
                Test("manual saves require correct phase, tolerance and stable samples", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile());
                    Throws(e.SaveCurrent, "Idle save allowed");
                    e.StartManualCurrent();
                    Until(e, () => e.State == "Manual current", 25);
                    Ticks(e, 10);
                    Throws(e.SaveCurrent, "Out-of-tolerance save allowed");
                    e.AdjustCurrent(0.6);
                    Throws(e.SaveCurrent, "Save before settling allowed");
                    Until(e, () => e.CanSaveCurrent, 30);
                    e.SaveCurrent();
                    Until(e, () => !e.IsRunning, 10);
                    Assert(e.CurrentSaved && !e.VoltageSaved, "Manual flags wrong");
                    Assert(!e.Reading.LoadOn && !e.Reading.ChargerOn, "Manual completion left outputs on");
                });
                Test("manual voltage and cross-check follow current save", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile());
                    Throws(e.StartManualVoltage, "Voltage before current allowed");
                    Throws(e.StartCrossCheck, "Cross-check before saves allowed");
                    SaveManualCurrent(e);
                    string sessionId = e.RunId;
                    Throws(e.StartCrossCheck, "Cross-check before voltage allowed");
                    e.StartManualVoltage();
                    Until(e, () => e.State == "Manual voltage", 15);
                    Assert(e.Reading.LoadMode == "CC" && e.Reading.LoadSetpoint == 1, "Wrong voltage calibration load");
                    e.AdjustVoltage(58.4 * 0.03);
                    Until(e, () => e.CanSaveVoltage, 30); e.SaveVoltage();
                    Until(e, () => !e.IsRunning, 10);
                    e.StartCrossCheck();
                    Assert(e.RunId == sessionId, "Manual calibration session identity changed between stages");
                    Assert(e.Checks.Count == 0, "Checks prematurely recorded");
                    Until(e, () => !e.IsRunning, 40);
                    Assert(e.State == "Simulation complete" && e.Checks.Count == 3, "Manual cross-check failed");
                });
                Test("manual adjustments are bounded and unavailable in auto", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    Throws(() => e.AdjustCurrent(0.1), "Manual trim allowed during auto"); e.Abort();
                    e.StartManualCurrent(); Until(e, () => e.State == "Manual current", 25);
                    Throws(() => e.AdjustCurrent(Double.NaN), "NaN trim accepted");
                    Throws(() => e.AdjustCurrent(100), "Unbounded trim accepted");
                    Assert(e.CurrentTrim == 0, "Rejected trim changed model");
                });
                Test("abort is immediate, idempotent and prevents further progress", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    Until(e, () => e.Reading.ChargerOn, 30); e.Abort(); e.Abort(); Ticks(e, 100);
                    Assert(!e.IsRunning && !e.Reading.ChargerOn && !e.Reading.LoadOn, "Abort failed");
                    Assert(!e.CurrentSaved && e.Checks.Count == 0, "Aborted work gained results");
                    Throws(e.SaveCurrent, "Save after abort allowed");
                });
                Test("new run invalidates earlier calibration/check results", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    Until(e, () => !e.IsRunning, 240); e.StartManualCurrent();
                    Assert(!e.CurrentSaved && !e.VoltageSaved && e.Checks.Count == 0, "Old results reused");
                });
                Test("limit violation stops simulation before another sample", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    e.Reading.Current = 17; e.Tick();
                    Assert(e.State == "Fault - stopped" && !e.IsRunning, "Overcurrent ignored");
                    Assert(e.Reading.Current == 0 && !e.Reading.LoadOn, "Fault left outputs on");
                });
                Test("non-finite telemetry faults instead of becoming a result", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    e.Reading.Voltage = Double.NaN; e.Tick();
                    Assert(e.State == "Fault - stopped" && e.Checks.Count == 0, "NaN telemetry accepted");
                });
                Test("manual operation has a bounded timeout", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartManualCurrent(); Ticks(e, 3602);
                    Assert(e.State == "Fault - stopped" && !e.IsRunning, "Manual timeout failed");
                });
                Test("stalled update loop stops outputs", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    Thread.Sleep(3100); e.Tick();
                    Assert(e.State == "Fault - stopped" && !e.IsRunning, "Stale update not detected");
                });
                Test("report identifies simulated and unverified results", () => {
                    CalibrationEngine e = new CalibrationEngine(Profile()); e.StartAuto();
                    Until(e, () => !e.IsRunning, 240);
                    string path = Path.GetTempFileName();
                    try
                    {
                        e.ExportReport(path); string data = File.ReadAllText(path);
                        Assert(data.Contains("SIMULATION ONLY - NOT A HARDWARE CALIBRATION CERTIFICATE"), "Missing report warning");
                        Assert(data.Contains("End-of-charge termination") && data.Contains("NOT TESTED"), "Unverified tests hidden");
                        Assert(!data.Contains("PASS"), "Simulation represented as hardware pass");
                        Assert(data.Contains("Power limit") && data.Contains("Current tolerance"), "Report missing profile evidence");
                    }
                    finally { File.Delete(path); }
                });
                Console.WriteLine(passed + " checks passed.");
                return 0;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("FAILED: " + error);
                return 1;
            }
        }
    }
}
