// Offline Windows fixture: legacy Host -> external worker -> real FFmpeg writer.
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

class UpgradeHost {
    static string Quote(string value) { return "\"" + value + "\""; }
    static void Main(string[] args) {
        string output = args[2];
        if (args[0] == "--worker") {
            var info = new ProcessStartInfo(args[1],
                "-hide_banner -loglevel error -re -f lavfi -i sine=frequency=440 " +
                "-c:a pcm_s16le -y " + Quote(Path.Combine(output, "writing.wav")));
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            var ffmpeg = Process.Start(info);
            File.WriteAllText(Path.Combine(output, "worker.pid"), Process.GetCurrentProcess().Id.ToString());
            File.WriteAllText(Path.Combine(output, "ffmpeg.pid"), ffmpeg.Id.ToString());
        } else {
            File.WriteAllText(Path.Combine(output, "root.pid"), Process.GetCurrentProcess().Id.ToString());
            var info = new ProcessStartInfo(args[0],
                "--worker " + Quote(args[1]) + " " + Quote(output));
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            Process.Start(info);
        }
        Thread.Sleep(Timeout.Infinite);
    }
}
