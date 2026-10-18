// A PID is only a lookup key. All destructive operations use this retained,
// creation-time-validated kernel handle, including when the PID is recycled.
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public sealed class HostCleanupProcess : IDisposable {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern SafeProcessHandle OpenProcess(uint access, bool inherit, int pid);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetProcessTimes(SafeProcessHandle h, out long created, out long exited,
                                      out long kernel, out long user);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool TerminateProcess(SafeProcessHandle h, uint code);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint WaitForSingleObject(SafeProcessHandle h, uint milliseconds);
    readonly SafeProcessHandle handle;
    HostCleanupProcess(SafeProcessHandle value) { handle = value; }

    public static HostCleanupProcess Open(int pid, long creationTicks) {
        var h = OpenProcess(0x00100000 | 0x1000 | 0x0001, false, pid);
        if (h.IsInvalid) {
            int error = Marshal.GetLastWin32Error();
            h.Dispose();
            if (error == 87) return null; // Process no longer exists.
            throw new Win32Exception(error, "Cannot acquire cleanup handle for PID " + pid);
        }
        var process = new HostCleanupProcess(h);
        try {
            long created, exited, kernel, user;
            if (!GetProcessTimes(h, out created, out exited, out kernel, out user))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            // CIM datetime has microsecond precision; FILETIME has 100ns precision.
            if (DateTime.FromFileTimeUtc(created).Ticks / 10 != creationTicks / 10) {
                process.Dispose();
                return null;
            }
            return process;
        } catch { process.Dispose(); throw; }
    }
    public long ExitTicks {
        get {
            uint result = WaitForSingleObject(handle, 0);
            if (result == 258) return 0;
            if (result != 0) throw new Win32Exception(Marshal.GetLastWin32Error());
            long created, exited, kernel, user;
            if (!GetProcessTimes(handle, out created, out exited, out kernel, out user))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return DateTime.FromFileTimeUtc(exited).Ticks;
        }
    }
    public void Stop() {
        if (ExitTicks != 0) return;
        if (!TerminateProcess(handle, 1) && ExitTicks == 0)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        // TerminateProcess is asynchronous. The drain loop waits for the handle
        // to signal before accepting this process's final descendant scan.
    }
    public void Dispose() { handle.Dispose(); }
}
