// proccap: захват звука конкретного процесса (с дочерними) через WASAPI process loopback (Windows 10 2004+).
// Пишет в stdout сырой PCM: float32, 48 кГц, стерео, чередование каналов.
//   proccap.exe <pid> include   — только звук процесса и его детей
//   proccap.exe <pid> exclude   — весь звук системы, кроме процесса и его детей
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

static class Program
{
    const int SampleRate = 48000, Channels = 2;

    static int Main(string[] args)
    {
        if (args.Length < 2 || !uint.TryParse(args[0], out var pid))
        {
            Console.Error.WriteLine("usage: proccap <pid> include|exclude");
            return 2;
        }
        bool exclude = args[1] == "exclude";
        try
        {
            Run(pid, exclude);
            return 0;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine(e.Message);
            return 1;
        }
    }

    static void Run(uint pid, bool exclude)
    {
        var client = Activate(pid, exclude);
        var fmt = new WAVEFORMATEX
        {
            wFormatTag = 3,  // WAVE_FORMAT_IEEE_FLOAT
            nChannels = Channels,
            nSamplesPerSec = SampleRate,
            wBitsPerSample = 32,
            nBlockAlign = Channels * 4,
            nAvgBytesPerSec = SampleRate * Channels * 4,
            cbSize = 0,
        };
        const uint LOOPBACK = 0x00020000, EVENTCALLBACK = 0x00040000, AUTOCONVERTPCM = 0x80000000, SRC_DEFAULT_QUALITY = 0x08000000;
        Check(client.Initialize(0, LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY, 200000, 0, ref fmt, IntPtr.Zero));
        var evt = new AutoResetEvent(false);
        Check(client.SetEventHandle(evt.SafeWaitHandle.DangerousGetHandle()));
        var iid = typeof(IAudioCaptureClient).GUID;
        Check(client.GetService(ref iid, out var svc));
        var capture = (IAudioCaptureClient)svc;
        Check(client.Start());

        var stdout = Console.OpenStandardOutput();
        var buf = new byte[SampleRate * Channels * 4];
        while (true)
        {
            evt.WaitOne(100);
            while (true)
            {
                Check(capture.GetNextPacketSize(out var packet));
                if (packet == 0) break;
                Check(capture.GetBuffer(out var data, out var frames, out var flags, out _, out _));
                int bytes = (int)frames * Channels * 4;
                if (bytes > buf.Length) buf = new byte[bytes];
                if ((flags & 0x2) != 0) Array.Clear(buf, 0, bytes);  // AUDCLNT_BUFFERFLAGS_SILENT
                else Marshal.Copy(data, buf, 0, bytes);
                Check(capture.ReleaseBuffer(frames));
                try { stdout.Write(buf, 0, bytes); stdout.Flush(); }
                catch (IOException) { return; }  // читатель закрыл трубу — выходим
            }
        }
    }

    static IAudioClient Activate(uint pid, bool exclude)
    {
        var p = new AUDIOCLIENT_ACTIVATION_PARAMS
        {
            ActivationType = 1,  // AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            TargetProcessId = pid,
            ProcessLoopbackMode = exclude ? 1 : 0,
        };
        int size = Marshal.SizeOf<AUDIOCLIENT_ACTIVATION_PARAMS>();
        IntPtr blob = Marshal.AllocHGlobal(size);
        Marshal.StructureToPtr(p, blob, false);
        var pv = new PROPVARIANT { vt = 65 /* VT_BLOB */, cbSize = (uint)size, pBlobData = blob };
        IntPtr pvPtr = Marshal.AllocHGlobal(Marshal.SizeOf<PROPVARIANT>());
        Marshal.StructureToPtr(pv, pvPtr, false);

        var handler = new Completion();
        var iid = typeof(IAudioClient).GUID;
        Check(ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref iid, pvPtr, handler, out var op));
        if (!handler.Done.WaitOne(5000)) throw new TimeoutException("activation timeout");
        Check(op.GetActivateResult(out var hr, out var iface));
        Check(hr);
        Marshal.FreeHGlobal(pvPtr);
        Marshal.FreeHGlobal(blob);
        return (IAudioClient)iface;
    }

    static void Check(int hr)
    {
        if (hr < 0) throw new COMException($"HRESULT 0x{hr:X8}", hr);
    }

    [DllImport("Mmdevapi.dll", ExactSpelling = true)]
    static extern int ActivateAudioInterfaceAsync(
        [MarshalAs(UnmanagedType.LPWStr)] string deviceInterfacePath, ref Guid riid, IntPtr activationParams,
        IActivateAudioInterfaceCompletionHandler completionHandler, out IActivateAudioInterfaceAsyncOperation op);

    [StructLayout(LayoutKind.Sequential)]
    struct AUDIOCLIENT_ACTIVATION_PARAMS
    {
        public int ActivationType;
        public uint TargetProcessId;
        public int ProcessLoopbackMode;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROPVARIANT
    {
        public ushort vt, r1, r2, r3;
        public uint cbSize;
        public IntPtr pBlobData;
    }

    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    struct WAVEFORMATEX
    {
        public ushort wFormatTag, nChannels;
        public uint nSamplesPerSec, nAvgBytesPerSec;
        public ushort nBlockAlign, wBitsPerSample, cbSize;
    }

    [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceAsyncOperation
    {
        [PreserveSig] int GetActivateResult(out int activateResult, [MarshalAs(UnmanagedType.IUnknown)] out object activatedInterface);
    }

    [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceCompletionHandler
    {
        [PreserveSig] int ActivateCompleted(IActivateAudioInterfaceAsyncOperation op);
    }

    // IAgileObject: без него WASAPI отказывается звать обработчик из своего потока
    [ComImport, Guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAgileObject { }

    sealed class Completion : IActivateAudioInterfaceCompletionHandler, IAgileObject
    {
        public readonly ManualResetEvent Done = new(false);
        public int ActivateCompleted(IActivateAudioInterfaceAsyncOperation op) { Done.Set(); return 0; }
    }

    [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        [PreserveSig] int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, ref WAVEFORMATEX format, IntPtr sessionGuid);
        [PreserveSig] int GetBufferSize(out uint frames);
        [PreserveSig] int GetStreamLatency(out long latency);
        [PreserveSig] int GetCurrentPadding(out uint padding);
        [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closest);
        [PreserveSig] int GetMixFormat(out IntPtr format);
        [PreserveSig] int GetDevicePeriod(out long def, out long min);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr handle);
        [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
        [PreserveSig] int ReleaseBuffer(uint frames);
        [PreserveSig] int GetNextPacketSize(out uint frames);
    }
}
