// x86 SDK bridge. No DLL is loaded until an explicit open request.
// ABI and timing bytes are recovered from the supplied application's metadata.
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;

internal static class CanReceiveBridge
{
    const string Protocol = "leanwatts-can/1";
    const int Capacity = 256;
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 524288, RecursionLimit = 12 };
    [StructLayout(LayoutKind.Explicit, Size=32)] struct InitConfig {
        [FieldOffset(0)] public uint Type;
        [FieldOffset(4)] public uint Code;
        [FieldOffset(8)] public uint Mask;
        [FieldOffset(12)] public uint Reserved;
        [FieldOffset(16)] public byte Filter;
        [FieldOffset(17)] public byte Timing0;
        [FieldOffset(18)] public byte Timing1;
        [FieldOffset(19)] public byte Mode;
    }
    [StructLayout(LayoutKind.Explicit, Size=16)] struct Frame {
        [FieldOffset(0)] public uint Id;
        [FieldOffset(4)] public byte Dlc;
        [FieldOffset(8)] public ulong Payload;
    }
    [StructLayout(LayoutKind.Explicit, Size=24)] struct ReceiveFrame {
        [FieldOffset(0)] public Frame Frame;
        [FieldOffset(16)] public ulong Timestamp;
    }
    [StructLayout(LayoutKind.Explicit, Size=20)] struct TransmitFrame {
        [FieldOffset(0)] public Frame Frame;
        [FieldOffset(16)] public uint Type;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Ansi, SetLastError=true)] static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool SetDllDirectory(string directory);
    [DllImport("kernel32.dll")] static extern bool FreeLibrary(IntPtr module);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate IntPtr OpenDevice(uint type, uint index, uint reserved);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate uint DeviceOp(IntPtr device);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate IntPtr InitCan(IntPtr device, uint channel, IntPtr config);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate uint ReceiveCan(IntPtr channel, IntPtr data, uint capacity, int wait);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate uint TransmitCan(IntPtr channel, IntPtr data, uint count);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate IntPtr GetProperty(IntPtr device);
    // UnmanagedFunctionPointerAttribute value 1 in the supplied managed wrapper:
    // the IProperty callbacks are Cdecl even though the exported APIs are StdCall.
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int SetPropertyValue(
        [MarshalAs(UnmanagedType.LPStr)] string path, [MarshalAs(UnmanagedType.LPStr)] string value);
    static IntPtr library, device, channel;
    static OpenDevice openDevice;
    static DeviceOp closeDevice, startCan, resetCan;
    static InitCan initCan;
    static ReceiveCan receiveCan;
    static TransmitCan transmitCan;
    static GetProperty getProperty;
    static SetPropertyValue propertySetter;
    static bool started, listenOnly = true;
    static uint channelIndex;
    static string sdkDirectory;

    static T Bind<T>(string name) where T : class {
        IntPtr symbol = GetProcAddress(library, name);
        if (symbol == IntPtr.Zero) throw new InvalidOperationException("SDK export is missing: " + name);
        return (T)(object)Marshal.GetDelegateForFunctionPointer(symbol, typeof(T));
    }
    static void LoadSdk() {
        if (library != IntPtr.Zero) return;
        if (IntPtr.Size != 4) throw new InvalidOperationException("The vendor SDK requires the x86 bridge.");
        string dll = Path.Combine(sdkDirectory, "zlgcan.dll");
        if (!File.Exists(dll)) throw new FileNotFoundException("zlgcan.dll is missing from the selected SDK folder.");
        if (!File.Exists(Path.Combine(sdkDirectory, "kerneldlls", "USBCAN.dll")))
            throw new FileNotFoundException("The SDK kerneldlls/USBCAN.dll dependency is missing.");
        Directory.SetCurrentDirectory(sdkDirectory);
        if (!SetDllDirectory(sdkDirectory)) throw new InvalidOperationException("Cannot set SDK dependency directory.");
        library = LoadLibraryEx(dll, IntPtr.Zero, 8); // LOAD_WITH_ALTERED_SEARCH_PATH, absolute local SDK path.
        if (library == IntPtr.Zero) throw new InvalidOperationException("Cannot load the x86 SDK (Windows error " + Marshal.GetLastWin32Error() + "). Check the supplied DLLs and VC++ runtimes.");
        openDevice=Bind<OpenDevice>("ZCAN_OpenDevice"); closeDevice=Bind<DeviceOp>("ZCAN_CloseDevice");
        initCan=Bind<InitCan>("ZCAN_InitCAN"); startCan=Bind<DeviceOp>("ZCAN_StartCAN");
        resetCan=Bind<DeviceOp>("ZCAN_ResetCAN"); receiveCan=Bind<ReceiveCan>("ZCAN_Receive");
        transmitCan=Bind<TransmitCan>("ZCAN_Transmit");
        getProperty=Bind<GetProperty>("GetIProperty");
    }
    static void CloseHandles() {
        IntPtr oldChannel=channel, oldDevice=device;
        channel=IntPtr.Zero; device=IntPtr.Zero; started=false; listenOnly=true;propertySetter=null;
        uint resetStatus=1,closeStatus=1;
        try { if (oldChannel!=IntPtr.Zero && resetCan!=null) resetStatus=resetCan(oldChannel); }
        finally { if (oldDevice!=IntPtr.Zero && closeDevice!=null) closeStatus=closeDevice(oldDevice); }
        if(resetStatus!=1 || closeStatus!=1)throw new InvalidOperationException("SDK cleanup failed; adapter or charger state is uncertain.");
    }
    static object Value(Dictionary<string,object> args, string name) {
        object value; if (!args.TryGetValue(name,out value)) throw new ArgumentException("Missing field: " + name); return value;
    }
    static long Integer(Dictionary<string,object> args, string name, long min, long max) {
        object value=Value(args,name);
        if (!(value is int) && !(value is long)) throw new ArgumentException(name + " must be an integer.");
        long number=Convert.ToInt64(value); if(number<min || number>max) throw new ArgumentException(name + " is outside its supported range."); return number;
    }
    static bool Boolean(Dictionary<string,object> args, string name) {
        object value=Value(args,name); if(!(value is bool)) throw new ArgumentException(name + " must be boolean."); return (bool)value;
    }
    static string State { get { return started ? (listenOnly ? "listening" : "active") : (device!=IntPtr.Zero ? "open" : "closed"); } }
    static Dictionary<string,object> Result() { return new Dictionary<string,object>{{"state",State},{"listen_only",listenOnly}}; }
    static byte[] ValidateFrame(Dictionary<string,object> frame) {
        if(Integer(frame,"id",0,0x1fffffff)!=0x1806e550 || !Boolean(frame,"extended") || Boolean(frame,"remote"))
            throw new ArgumentException("Only the recovered charger extended data ID is permitted.");
        object error; if(frame.TryGetValue("error",out error) && (!(error is bool) || (bool)error)) throw new ArgumentException("Error frames cannot be sent.");
        int dlc=(int)Integer(frame,"dlc",2,4);
        IList source=Value(frame,"data") as IList;
        if(source==null || source.Count!=dlc || (dlc!=2 && dlc!=4)) throw new ArgumentException("Invalid command length.");
        byte[] data=new byte[dlc];
        for(int i=0;i<dlc;i++) {
            if(!(source[i] is int) || (int)source[i]<0 || (int)source[i]>255) throw new ArgumentException("Invalid data byte.");
            data[i]=(byte)(int)source[i];
        }
        if(data[0]!=0) throw new ArgumentException("Invalid command prefix.");
        int selector=data[1];
        if(dlc==2) { if(selector<0x17 || selector>0x19) throw new ArgumentException("Unknown save command."); return data; }
        int word=data[2]*256+data[3];
        bool valid=(selector>=1 && selector<=0x11 && selector!=0x0b && word<= (selector==0x0c ? 255 : 32767))
            || (selector==0x0b && word>=1 && word<=6)
            || (selector==0x14 && (word==0 || word==1 || word==128))
            || ((selector==0x15 || selector==0x16) && word<=10000);
        if(!valid) throw new ArgumentException("Command is outside the recovered protocol whitelist.");
        return data;
    }
    static object Execute(string action, Dictionary<string,object> args) {
        if(action=="open") {
            if(device!=IntPtr.Zero) throw new InvalidOperationException("Device is already open.");
            uint index=(uint)Integer(args,"index",0,31);
            channelIndex=(uint)Integer(args,"channel",0,0); // supplied USBCAN-I has one channel
            LoadSdk(); device=openDevice(3,index,0);
            if(device==IntPtr.Zero || device==new IntPtr(-1)) {device=IntPtr.Zero;throw new InvalidOperationException("SDK could not open USBCAN1. Check the driver and close the Chinese application before connecting.");}
            var result=Result();result["device_type"]=3;result["index"]=index;result["channel"]=channelIndex;return result;
        }
        if(action=="close") { CloseHandles();return Result(); }
        if(device==IntPtr.Zero) throw new InvalidOperationException("Open the device first.");
        if(action=="reset") {
            if(channel!=IntPtr.Zero) { uint status=resetCan(channel);channel=IntPtr.Zero;started=false;listenOnly=true;if(status!=1)throw new InvalidOperationException("SDK reset failed; connection state is uncertain."); }
            return Result();
        }
        if(action=="start") {
            if(channel!=IntPtr.Zero) throw new InvalidOperationException("Reset CAN before starting again.");
            int rate=(int)Integer(args,"bitrate",5000,1000000);
            int[] rates={1000000,800000,500000,250000,125000,100000,50000,20000,10000,5000};
            byte[] t0={0,0,0,1,3,4,9,24,49,191},t1={20,22,28,28,28,28,28,28,28,255};
            int timing=Array.IndexOf(rates,rate);if(timing<0)throw new ArgumentException("Unsupported CAN bitrate.");
            listenOnly=Boolean(args,"listen_only");
            // Original buttonConnect_Click IL009D..010A performs this step
            // unconditionally; classic timing bytes alone are not equivalent.
            if(propertySetter==null) {
                IntPtr property=getProperty(device);
                if(property==IntPtr.Zero)throw new InvalidOperationException("SDK did not provide its property interface.");
                IntPtr setter=Marshal.ReadIntPtr(property); // IProperty.SetValue is first of three x86 function pointers.
                if(setter==IntPtr.Zero)throw new InvalidOperationException("SDK property setter is unavailable.");
                propertySetter=(SetPropertyValue)Marshal.GetDelegateForFunctionPointer(setter,typeof(SetPropertyValue));
            }
            if(propertySetter(channelIndex.ToString(System.Globalization.CultureInfo.InvariantCulture)+"/baud_rate",rate.ToString(System.Globalization.CultureInfo.InvariantCulture))!=1)
                throw new InvalidOperationException("SDK could not set the selected CAN bitrate.");
            InitConfig config=new InitConfig {Type=0,Code=0,Mask=0xffffffff,Filter=1,Timing0=t0[timing],Timing1=t1[timing],Mode=(byte)(listenOnly?1:0)};
            IntPtr memory=Marshal.AllocHGlobal(32);
            try {
                // Explicitly zero the unused union tail before writing fields.
                Marshal.Copy(new byte[32],0,memory,32);
                Marshal.WriteInt32(memory,0,unchecked((int)config.Type));
                Marshal.WriteInt32(memory,4,unchecked((int)config.Code));
                Marshal.WriteInt32(memory,8,unchecked((int)config.Mask));
                Marshal.WriteByte(memory,16,config.Filter);Marshal.WriteByte(memory,17,config.Timing0);
                Marshal.WriteByte(memory,18,config.Timing1);Marshal.WriteByte(memory,19,config.Mode);
                channel=initCan(device,channelIndex,memory);
            }
            finally {Marshal.FreeHGlobal(memory);}
            if(channel==IntPtr.Zero || channel==new IntPtr(-1)) {channel=IntPtr.Zero;throw new InvalidOperationException("SDK CAN initialization failed.");}
            if(startCan(channel)!=1)throw new InvalidOperationException("SDK CAN start failed.");
            started=true;var result=Result();result["bitrate"]=rate;return result;
        }
        if(!started || channel==IntPtr.Zero) throw new InvalidOperationException("Start CAN first.");
        if(action=="receive") {
            IntPtr memory=Marshal.AllocHGlobal(24*Capacity);
            var frames=new List<object>(); int malformed=0;
            try {
                uint count=receiveCan(channel,memory,Capacity,0);
                if(count>Capacity)throw new InvalidOperationException("SDK receive returned an invalid count.");
                for(int i=0;i<count;i++) {
                    IntPtr item=IntPtr.Add(memory,i*24);uint raw=unchecked((uint)Marshal.ReadInt32(item));int dlc=Marshal.ReadByte(item,4);
                    if(dlc>8) { malformed++;continue; }
                    int[] data=new int[dlc];for(int j=0;j<dlc;j++)data[j]=Marshal.ReadByte(item,8+j);
                    frames.Add(new Dictionary<string,object>{{"id",raw&0x1fffffff},{"extended",(raw&0x80000000)!=0},{"remote",(raw&0x40000000)!=0},{"error",(raw&0x20000000)!=0},{"dlc",dlc},{"data",data},{"timestamp_us",unchecked((ulong)Marshal.ReadInt64(item,16))}});
                }
            } finally {Marshal.FreeHGlobal(memory);}
            var result=Result();result["frames"]=frames;result["dropped_malformed"]=malformed;return result;
        }
        if(action=="send") {
            if(listenOnly)throw new InvalidOperationException("Transmit is unavailable in listen-only mode.");
            var frame=Value(args,"frame") as Dictionary<string,object>;
            if(frame==null)throw new ArgumentException("Missing frame object.");
            byte[] data=ValidateFrame(frame); IntPtr memory=Marshal.AllocHGlobal(20);
            uint accepted;
            try {
                Marshal.Copy(new byte[20],0,memory,20);
                Marshal.WriteInt32(memory,unchecked((int)0x9806e550));Marshal.WriteByte(memory,4,(byte)data.Length);
                Marshal.Copy(data,0,IntPtr.Add(memory,8),data.Length);accepted=transmitCan(channel,memory,1);
            } finally {Marshal.FreeHGlobal(memory);}
            if(accepted!=1)throw new InvalidOperationException("SDK did not accept the frame; charger state is uncertain.");
            var result=Result();result["accepted"]=1;result["acknowledged"]=false;
            result["scope"]="CAN API accepted frame; charger acknowledgement and persistence are unverified.";return result;
        }
        throw new ArgumentException("Unknown action.");
    }
    static void AssertLayout() {
        if(Marshal.SizeOf(typeof(InitConfig))!=32 || Marshal.SizeOf(typeof(Frame))!=16 || Marshal.SizeOf(typeof(ReceiveFrame))!=24 || Marshal.SizeOf(typeof(TransmitFrame))!=20
           || Marshal.OffsetOf(typeof(InitConfig),"Mode").ToInt32()!=19 || Marshal.OffsetOf(typeof(ReceiveFrame),"Timestamp").ToInt32()!=16)
            throw new InvalidOperationException("CAN ABI layout mismatch.");
    }
    static void AssertWhitelist() {
        string[] valid={"[0,20,0,0]","[0,20,0,1]","[0,20,0,128]","[0,23]","[0,24]","[0,25]","[0,11,0,6]","[0,21,39,16]","[0,12,0,255]","[0,17,127,255]"};
        string[] invalid={"[0,20,0,2]","[0,22,39,17]","[0,11,0,7]","[0,18,0,0]","[0,12,1,0]","[0,23,0,0]","[0,1]","[1,23]"};
        foreach(string data in valid) {
            var frame=Json.Deserialize<Dictionary<string,object>>("{\"id\":403105104,\"extended\":true,\"remote\":false,\"dlc\":"+(data.Split(',').Length)+",\"data\":"+data+"}");
            ValidateFrame(frame);
        }
        foreach(string data in invalid) {
            var frame=Json.Deserialize<Dictionary<string,object>>("{\"id\":403105104,\"extended\":true,\"remote\":false,\"dlc\":"+(data.Split(',').Length)+",\"data\":"+data+"}");
            bool rejected=false;try {ValidateFrame(frame);}catch(ArgumentException){rejected=true;}
            if(!rejected)throw new InvalidOperationException("Native command whitelist test failed.");
        }
    }
    static string ReadLineBounded() {
        StringBuilder line=new StringBuilder();int value;
        while((value=Console.In.Read())!=-1) {if(value==10)return line.ToString().TrimEnd('\r');if(line.Length>=8192)throw new InvalidDataException("Request exceeds limit.");line.Append((char)value);}
        return line.Length==0 ? null : line.ToString();
    }
    static int Main(string[] arguments) {
        Console.InputEncoding=new UTF8Encoding(false);Console.OutputEncoding=new UTF8Encoding(false);
        try {
            AssertLayout();
            if(arguments.Length==1 && arguments[0]=="--self-test") {AssertWhitelist();Console.WriteLine(Json.Serialize(new {ok=true,abi=new[]{32,16,24,20},whitelist_vectors=18,x86=IntPtr.Size==4,hardware_accessed=false}));return 0;}
            if(arguments.Length!=2 || arguments[0]!="--sdk-dir")throw new ArgumentException("Use --sdk-dir with the exact local SDK directory.");
            sdkDirectory=Path.GetFullPath(arguments[1]);
            string nonce=null,line;long lastSequence=0;
            while((line=ReadLineBounded())!=null) {
                if(line.Length==0)continue;
                var request=Json.Deserialize<Dictionary<string,object>>(line);
                string requestNonce=Value(request,"nonce") as string;Guid parsed;
                if((Value(request,"protocol") as string)!=Protocol || requestNonce==null || !Guid.TryParseExact(requestNonce,"N",out parsed))throw new InvalidDataException("Invalid protocol envelope.");
                long sequence=Integer(request,"seq",1,int.MaxValue);
                if(nonce==null)nonce=requestNonce;
                if(nonce!=requestNonce || sequence!=lastSequence+1)throw new InvalidDataException("Protocol sequence mismatch.");
                lastSequence=sequence;
                var response=new Dictionary<string,object>{{"protocol",Protocol},{"nonce",nonce},{"seq",sequence}};
                try {
                    string action=Value(request,"action") as string;
                    var args=Value(request,"args") as Dictionary<string,object>;
                    if(action==null || args==null)throw new ArgumentException("Invalid action arguments.");
                    response["result"]=Execute(action,args);response["ok"]=true;
                } catch(Exception error) {
                    try {CloseHandles();} catch(Exception) { }
                    response["ok"]=false;response["error"]=error.GetType().Name+": "+error.Message;
                }
                Console.WriteLine(Json.Serialize(response));Console.Out.Flush();
            }
            return 0;
        } catch(Exception error) {Console.Error.WriteLine(error.GetType().Name+": "+error.Message);return 1;}
        finally {try {CloseHandles();}catch(Exception){} if(library!=IntPtr.Zero)FreeLibrary(library);}
    }
}
